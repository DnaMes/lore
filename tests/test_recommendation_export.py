"""Evidence-backed harness recommendations (#189).

The fixture has a positive observation (one project, two sessions, one tool
each), a correction and a contradiction for the same recommendation, and a
rule that only appears in a second project — so scope derivation has a real
case for ``project`` and for ``global``.

Every assertion is about the exported artifact: nothing here applies a rule to
a harness, and the tests pin that it does not.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.utils.rules import (
    STORE_FORMAT,
    activate_recommendation,
    build_recommendations,
    load_recommendations,
    merge_recommendations,
    recommendations_path,
    record_evaluation,
    save_recommendations,
)

T0 = datetime(2026, 4, 1, 12, 0, 0)

LINT_RULE = "You should always run the linter before committing changes."
BRANCH_RULE = "Never commit directly to the main branch of this repository."
PATH_RULE = "Avoid hardcoding absolute paths in tests and scripts."
SECRET_RULE = (
    "You should never paste OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz012345 into a prompt."
)


def _message(role, content):
    return UnifiedMessage(role=role, content=content, timestamp=T0)


def _session(session_id, tool, project, rule_sentence, *, origin="interactive"):
    return UnifiedSession(
        tool=tool,
        session_id=session_id,
        created_at=T0,
        last_updated=T0 + timedelta(minutes=5),
        messages=[
            _message(Role.USER, "Help me with the build"),
            _message(Role.ASSISTANT, rule_sentence),
        ],
        origin=origin,
        project_path=project,
    )


def fixture_sessions():
    return [
        _session("s1", Tool.CLAUDE_CODE, "/repo/one", LINT_RULE),
        _session("s2", Tool.CODEX, "/repo/one", LINT_RULE),
        _session("s3", Tool.CLAUDE_CODE, "/repo/two", BRANCH_RULE),
        _session("s4", Tool.CURSOR, "/repo/one", PATH_RULE),
    ]


def _by_text(recommendations):
    return {recommendation.text: recommendation for recommendation in recommendations}


# ---------------------------------------------------------------------------
# export shape
# ---------------------------------------------------------------------------


def test_export_shape_is_machine_readable():
    recommendations = build_recommendations(fixture_sessions(), now="2026-04-02T00:00:00+00:00")

    assert recommendations
    payload = recommendations[0].as_dict()
    assert set(payload) == {
        "id",
        "text",
        "scope",
        "scope_target",
        "state",
        "created_at",
        "activated_by",
        "activated_at",
        "current_verdict",
        "generation",
        "evidence",
        "history",
    }
    assert payload["id"].startswith("rec-")
    assert payload["scope"] in {"project", "tool", "global"}
    assert payload["generation"] == {
        "provider": "local",
        "model": "sentence-keyword-v1",
        "prompt_version": "rules-extract-v1",
    }
    assert payload["created_at"] == "2026-04-02T00:00:00+00:00"
    assert payload["current_verdict"] == "unreviewed"


def test_evidence_links_carry_permalinks():
    recommendations = _by_text(build_recommendations(fixture_sessions()))

    lint = recommendations[LINT_RULE]
    assert [link.permalink for link in lint.evidence] == ["session:s1#m1", "session:s2#m1"]
    assert [(link.session_id, link.tool, link.message_seq) for link in lint.evidence] == [
        ("s1", "claude-code", 1),
        ("s2", "codex", 1),
    ]


def test_scope_is_derived_from_evidence():
    recommendations = _by_text(build_recommendations(fixture_sessions()))

    # same project, two different tools → project scope
    assert (recommendations[LINT_RULE].scope, recommendations[LINT_RULE].scope_target) == (
        "project",
        "/repo/one",
    )
    assert (recommendations[BRANCH_RULE].scope, recommendations[BRANCH_RULE].scope_target) == (
        "project",
        "/repo/two",
    )
    assert (recommendations[PATH_RULE].scope, recommendations[PATH_RULE].scope_target) == (
        "project",
        "/repo/one",
    )


def test_scope_falls_back_when_project_is_unknown():
    sessions = [_session("s9", Tool.CODEX, None, BRANCH_RULE)]

    recommendation = build_recommendations(sessions)[0]

    assert (recommendation.scope, recommendation.scope_target) == ("tool", "codex")


def test_ids_are_stable_across_reruns():
    first = [recommendation.id for recommendation in build_recommendations(fixture_sessions())]
    second = [
        recommendation.id
        for recommendation in build_recommendations(
            fixture_sessions(), now="2030-01-01T00:00:00+00:00"
        )
    ]

    assert first == second


# ---------------------------------------------------------------------------
# inactive by default
# ---------------------------------------------------------------------------


def test_inactive_by_default():
    recommendations = build_recommendations(fixture_sessions())

    assert [recommendation.state for recommendation in recommendations] == ["inactive"] * len(
        recommendations
    )
    assert all(recommendation.activated_by is None for recommendation in recommendations)
    assert all(recommendation.activated_at is None for recommendation in recommendations)
    assert all(recommendation.history == [] for recommendation in recommendations)


def test_export_does_not_modify_harness_configuration(tmp_path, monkeypatch, capsys):
    store = tmp_path / "store"
    harness = tmp_path / "repo"
    (harness / ".claude").mkdir(parents=True)
    agents_md = harness / "AGENTS.md"
    agents_md.write_text("# House rules\n\nHand-written.\n", encoding="utf-8")
    settings = harness / ".claude" / "settings.json"
    settings.write_text(json.dumps({"permissions": {"deny": []}}), encoding="utf-8")

    _run_cli(
        monkeypatch, ["--output-dir", str(store), "rules", "export", "--recommendations", "--json"]
    )
    capsys.readouterr()

    assert agents_md.read_text(encoding="utf-8") == "# House rules\n\nHand-written.\n"
    assert json.loads(settings.read_text(encoding="utf-8")) == {"permissions": {"deny": []}}
    assert sorted(path.name for path in harness.rglob("*")) == [
        ".claude",
        "AGENTS.md",
        "settings.json",
    ]
    # the only artifact is the recommendation store
    assert sorted(path.name for path in store.iterdir()) == ["recommendations.json"]


# ---------------------------------------------------------------------------
# activation
# ---------------------------------------------------------------------------


def test_activation_is_recorded(tmp_path):
    path = tmp_path / "recommendations.json"
    recommendations = build_recommendations(fixture_sessions())
    save_recommendations(path, recommendations)
    target = recommendations[0]

    activated = activate_recommendation(
        recommendations, target.id, by="andre", now="2026-04-03T08:00:00+00:00"
    )
    save_recommendations(path, recommendations)

    assert activated.state == "active"
    assert activated.activated_by == "andre"
    assert activated.activated_at == "2026-04-03T08:00:00+00:00"
    reloaded = {item.id: item for item in load_recommendations(path)}
    assert reloaded[target.id].state == "active"
    assert reloaded[target.id].activated_by == "andre"


def test_activation_requires_a_known_id():
    recommendations = build_recommendations(fixture_sessions())

    with pytest.raises(KeyError):
        activate_recommendation(recommendations, "rec-nope", by="andre")


def test_activation_persists_across_reexport(tmp_path):
    path = recommendations_path(tmp_path)
    fresh = build_recommendations(fixture_sessions())
    save_recommendations(path, fresh)
    activate_recommendation(fresh, fresh[0].id, by="andre", now="2026-04-03T08:00:00+00:00")
    save_recommendations(path, fresh)

    merged = merge_recommendations(
        load_recommendations(path), build_recommendations(fixture_sessions())
    )
    save_recommendations(path, merged)

    stored = {item.id: item for item in load_recommendations(path)}
    assert stored[fresh[0].id].state == "active"
    assert stored[fresh[0].id].activated_by == "andre"
    assert all(item.state == "inactive" for item in stored.values() if item.id != fresh[0].id)


# ---------------------------------------------------------------------------
# corrections and contradictions
# ---------------------------------------------------------------------------


def test_correction_is_appended_not_replaced():
    recommendations = build_recommendations(fixture_sessions())
    target = _by_text(recommendations)[LINT_RULE]
    original_text = target.text
    original_evidence = list(target.evidence)

    record_evaluation(
        recommendations,
        target.id,
        "corrected",
        by="andre",
        note="Only applies to Python files",
        now="2026-04-04T10:00:00+00:00",
    )

    assert target.text == original_text  # the original is never rewritten
    assert target.evidence == original_evidence
    assert len(target.history) == 1
    entry = target.history[0]
    assert (entry.verdict, entry.by, entry.at, entry.note) == (
        "corrected",
        "andre",
        "2026-04-04T10:00:00+00:00",
        "Only applies to Python files",
    )
    assert target.current_verdict == "corrected"


def test_contradiction_appends_and_updates_visible_status():
    recommendations = build_recommendations(fixture_sessions())
    target = _by_text(recommendations)[BRANCH_RULE]

    record_evaluation(
        recommendations, target.id, "accepted", by="andre", now="2026-04-04T10:00:00+00:00"
    )
    record_evaluation(
        recommendations,
        target.id,
        "contradicted",
        by="sam",
        note="Repo switched to trunk-based flow",
        now="2026-04-05T10:00:00+00:00",
    )

    assert [entry.verdict for entry in target.history] == ["accepted", "contradicted"]
    assert target.current_verdict == "contradicted"
    assert target.as_dict()["current_verdict"] == "contradicted"
    # the contradiction does not deactivate or delete anything on its own
    assert target.state == "inactive"
    assert target.text == BRANCH_RULE


def test_invalid_verdict_rejected():
    recommendations = build_recommendations(fixture_sessions())

    with pytest.raises(ValueError, match="invalid verdict"):
        record_evaluation(recommendations, recommendations[0].id, "maybe", by="andre")


def test_history_survives_round_trip(tmp_path):
    path = recommendations_path(tmp_path)
    recommendations = build_recommendations(fixture_sessions())
    target = recommendations[0]
    activate_recommendation(recommendations, target.id, by="andre", now="2026-04-03T08:00:00+00:00")
    record_evaluation(
        recommendations,
        target.id,
        "corrected",
        by="andre",
        note="narrow it",
        now="2026-04-04T10:00:00+00:00",
    )
    save_recommendations(path, recommendations)

    stored = {item.id: item for item in load_recommendations(path)}[target.id]

    assert stored.state == "active"
    assert stored.activated_by == "andre"
    assert [entry.as_dict() for entry in stored.history] == [
        {
            "verdict": "corrected",
            "at": "2026-04-04T10:00:00+00:00",
            "by": "andre",
            "note": "narrow it",
        }
    ]
    assert [link.permalink for link in stored.evidence] == [
        link.permalink for link in target.evidence
    ]


def test_merge_keeps_evidence_and_state():
    existing = build_recommendations(fixture_sessions())
    target = existing[0]
    activate_recommendation(existing, target.id, by="andre", now="2026-04-03T08:00:00+00:00")
    # a re-scan that lost one supporting session must not lose its evidence link
    shrunk = [_session("s1", Tool.CLAUDE_CODE, "/repo/one", LINT_RULE)]

    merged = merge_recommendations(existing, build_recommendations(shrunk))

    by_id = {item.id: item for item in merged}
    assert by_id[target.id].state == "active"
    permalinks = [link.permalink for item in merged for link in item.evidence]
    assert "session:s2#m1" in permalinks


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------


def test_no_secrets_in_export(tmp_path):
    sessions = [_session("s5", Tool.CLAUDE_CODE, "/repo/one", SECRET_RULE)]
    path = recommendations_path(tmp_path)
    payload = save_recommendations(path, build_recommendations(sessions))
    written = path.read_text(encoding="utf-8")

    assert "sk-proj-abcdefghijklmnopqrstuvwxyz012345" not in written
    assert "[REDACTED:openai_key]" in written
    assert "sk-proj" not in json.dumps(payload["recommendations"])


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
    monkeypatch.setattr(
        lore_cli, "get_all_extractors", lambda: [_FakeExtractor(fixture_sessions())]
    )
    monkeypatch.setattr("sys.argv", ["lore", *argv])
    return lore_cli.main()


def test_cli_export_json(tmp_path, monkeypatch, capsys):
    _run_cli(
        monkeypatch,
        ["--output-dir", str(tmp_path), "rules", "export", "--recommendations", "--json"],
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == STORE_FORMAT
    assert payload["format_version"] == 1
    recommendations = payload["recommendations"]
    assert len(recommendations) == 3
    assert {item["state"] for item in recommendations} == {"inactive"}
    assert all(item["evidence"] for item in recommendations)
    assert recommendations_path(tmp_path).exists()


def test_cli_export_without_recommendations_selector(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as excinfo:
        _run_cli(monkeypatch, ["--output-dir", str(tmp_path), "rules", "export"])

    assert excinfo.value.code == 1
    assert "--recommendations" in capsys.readouterr().err
    assert not recommendations_path(tmp_path).exists()


def test_cli_activate_and_evaluate(tmp_path, monkeypatch, capsys):
    _run_cli(monkeypatch, ["--output-dir", str(tmp_path), "rules", "export", "--recommendations"])
    capsys.readouterr()
    stored = load_recommendations(recommendations_path(tmp_path))
    target = stored[0]

    _run_cli(
        monkeypatch,
        ["--output-dir", str(tmp_path), "rules", "activate", target.id, "--by", "andre"],
    )
    activated = capsys.readouterr().out
    assert f"Activated {target.id}" in activated
    assert "No harness configuration was modified" in activated

    _run_cli(
        monkeypatch,
        [
            "--output-dir",
            str(tmp_path),
            "rules",
            "evaluate",
            target.id,
            "--verdict",
            "contradicted",
            "--note",
            "superseded by the new build",
            "--by",
            "sam",
        ],
    )
    capsys.readouterr()

    stored = {item.id: item for item in load_recommendations(recommendations_path(tmp_path))}
    assert stored[target.id].state == "active"
    assert stored[target.id].activated_by == "andre"
    assert stored[target.id].history[-1].verdict == "contradicted"
    assert stored[target.id].history[-1].by == "sam"
    assert stored[target.id].history[-1].note == "superseded by the new build"


def test_cli_review_is_read_only(tmp_path, monkeypatch, capsys):
    _run_cli(monkeypatch, ["--output-dir", str(tmp_path), "rules", "export", "--recommendations"])
    capsys.readouterr()
    before = recommendations_path(tmp_path).read_bytes()

    _run_cli(monkeypatch, ["--output-dir", str(tmp_path), "rules", "review"])

    reviewed = capsys.readouterr().out
    assert "Inactive until explicitly activated" in reviewed
    assert "session:s1#m1" in reviewed
    assert recommendations_path(tmp_path).read_bytes() == before


def test_cli_activate_unknown_id(tmp_path, monkeypatch, capsys):
    _run_cli(monkeypatch, ["--output-dir", str(tmp_path), "rules", "export", "--recommendations"])
    capsys.readouterr()

    with pytest.raises(SystemExit) as excinfo:
        _run_cli(monkeypatch, ["--output-dir", str(tmp_path), "rules", "activate", "rec-missing"])

    assert excinfo.value.code == 1
    assert "Cannot activate" in capsys.readouterr().err
