"""Tests for redacted export/sharing (issue #197, B3.3)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import lore_cli
from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.markdown import MarkdownExporter
from lore.exporters.redaction import REDACTED_EXPORT_MARKER, redact_session
from lore.interfaces import web as web_mod
from lore.interfaces import web_data
from lore.utils.paths import lore_home

_FAKE_OPENAI_KEY = "sk-" + "a" * 24


def _secret_session(session_id: str = "ses-redact-001") -> UnifiedSession:
    now = datetime(2026, 3, 4, 12, 0, 0)
    return UnifiedSession(
        tool=Tool.CODEX,
        session_id=session_id,
        created_at=now,
        last_updated=now,
        project_path="/projects/demo",
        title=f"Title holds {_FAKE_OPENAI_KEY}",
        messages=[
            UnifiedMessage(
                role=Role.USER,
                content=f"user said {_FAKE_OPENAI_KEY}",
                timestamp=now,
                reasoning=f"think {_FAKE_OPENAI_KEY}",
                tool_calls=[
                    {
                        "tool": "Bash",
                        "input": {"cmd": "curl", "token": _FAKE_OPENAI_KEY},
                    }
                ],
                tool_events=[{"name": "Bash", "detail": _FAKE_OPENAI_KEY}],
            ),
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="no secrets here",
                timestamp=now,
            ),
        ],
    )


def _export_ns(output_dir: str | Path, **extra) -> argparse.Namespace:
    return argparse.Namespace(
        output_dir=str(output_dir),
        tool=None,
        project=None,
        full=True,
        redact=extra.pop("redact", False),
        **extra,
    )


class _SingleExtractor:
    tool = Tool.CODEX

    def __init__(self, sessions: list[UnifiedSession]) -> None:
        self._sessions = sessions
        self.skipped_sources = set()

    def is_available(self) -> bool:
        return True

    def extract_sessions(self):
        yield from self._sessions


def test_redact_session_leaves_input_untouched_and_counts():
    session = _secret_session()
    original_title = session.title
    redacted, count = redact_session(session)

    assert session.title == original_title
    assert _FAKE_OPENAI_KEY in (session.messages[0].content or "")
    assert count >= 4
    assert _FAKE_OPENAI_KEY not in (redacted.title or "")
    assert _FAKE_OPENAI_KEY not in redacted.messages[0].content
    assert _FAKE_OPENAI_KEY not in (redacted.messages[0].reasoning or "")
    assert _FAKE_OPENAI_KEY not in json.dumps(redacted.messages[0].tool_calls)
    assert redacted.project_path == session.project_path
    assert "[REDACTED:" in (redacted.title or "")


def test_export_redact_refused_without_explicit_output_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    archive = lore_home()
    archive.mkdir(parents=True)

    rc = lore_cli.cmd_export(_export_ns(archive, redact=True))
    assert rc == 1
    assert "output-dir" in capsys.readouterr().err.lower()


def test_export_redact_refused_when_output_is_default_archive(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    archive = lore_home()
    archive.mkdir(parents=True)

    rc = lore_cli.cmd_export(_export_ns(str(archive), redact=True))
    assert rc == 1
    assert capsys.readouterr().err


def test_export_redact_writes_marker_and_redacted_markdown(tmp_path, monkeypatch, capsys):
    out = tmp_path / "share"
    session = _secret_session()
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_SingleExtractor([session])])

    rc = lore_cli.cmd_export(_export_ns(out, redact=True))
    assert rc in (None, 0)
    assert (out / REDACTED_EXPORT_MARKER).is_file()
    md_files = list(out.rglob("*.md"))
    assert md_files
    body = md_files[0].read_text(encoding="utf-8")
    assert _FAKE_OPENAI_KEY not in body
    assert "[REDACTED:" in body
    assert "Redacted" in capsys.readouterr().out


def test_plain_export_refused_in_redacted_directory(tmp_path, monkeypatch, capsys):
    out = tmp_path / "share"
    session = _secret_session("ses-redact-002")
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_SingleExtractor([session])])
    lore_cli.cmd_export(_export_ns(out, redact=True))

    rc = lore_cli.cmd_export(_export_ns(out, redact=False))
    assert rc == 1
    assert "redacted" in capsys.readouterr().err.lower()


def test_plain_export_markdown_byte_identical_to_baseline(tmp_path, monkeypatch):
    session = _secret_session("ses-plain-baseline")
    exporter = MarkdownExporter(tmp_path / "baseline")
    expected = exporter.export_session(session).read_bytes()

    out = tmp_path / "plain-out"
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_SingleExtractor([session])])
    lore_cli.cmd_export(_export_ns(out, redact=False))

    actual = next(out.rglob("*.md")).read_bytes()
    assert actual == expected


def test_export_html_redact_omits_secret(tmp_path, monkeypatch, capsys):
    session = _secret_session("ses-html-redact")
    monkeypatch.setattr(lore_cli, "_find_session_by_id", lambda _id: session)
    out = tmp_path / "out.html"
    rc = lore_cli.cmd_export_html(
        argparse.Namespace(session_id=session.session_id, output=str(out), redact=True)
    )
    assert rc == 0
    html = out.read_text(encoding="utf-8")
    assert _FAKE_OPENAI_KEY not in html
    assert "Redacted" in capsys.readouterr().err


def test_web_export_without_redact_unchanged(monkeypatch, tmp_path):
    session_id = "ses-web-plain"
    session = _secret_session(session_id)
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps({"stats": {}, "sessions": [{"id": session_id, "tool": "codex"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_mod, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_mod, "load_session_by_id", lambda sid, preferred_tool=None: session)

    with web_mod.app.test_client() as client:
        response = client.get(f"/export/{session_id}")

    assert response.status_code == 200
    assert _FAKE_OPENAI_KEY in response.get_data(as_text=True)
    assert "X-Lore-Redactions" not in response.headers


def test_web_export_redact_header_and_content(monkeypatch, tmp_path):
    session_id = "ses-web-redact"
    session = _secret_session(session_id)
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps({"stats": {}, "sessions": [{"id": session_id, "tool": "codex"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_data, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_mod, "INDEX_PATH", index_path)
    monkeypatch.setattr(web_mod, "load_session_by_id", lambda sid, preferred_tool=None: session)

    with web_mod.app.test_client() as client:
        response = client.get(f"/export/{session_id}?redact=1")

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert _FAKE_OPENAI_KEY not in body
    assert "[REDACTED:" in body
    assert int(response.headers["X-Lore-Redactions"]) >= 1


def test_export_redact_refused_below_the_live_archive(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [])
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    nested = lore_home() / "share"
    nested.mkdir(parents=True)

    rc = lore_cli.cmd_export(_export_ns(str(nested), redact=True))

    assert rc == 1
    assert not (nested / REDACTED_EXPORT_MARKER).exists()


def test_export_redact_refused_into_non_empty_plain_directory(tmp_path, monkeypatch, capsys):
    out = tmp_path / "plain"
    out.mkdir()
    (out / "old-session.md").write_text("plain export", encoding="utf-8")
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_SingleExtractor([])])

    rc = lore_cli.cmd_export(_export_ns(out, redact=True))

    assert rc == 1
    assert (out / "old-session.md").read_text(encoding="utf-8") == "plain export"


def test_redacted_index_json_has_no_secret_title(tmp_path, monkeypatch):
    out = tmp_path / "share"
    session = _secret_session("ses-redact-index")
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [_SingleExtractor([session])])

    lore_cli.cmd_export(_export_ns(out, redact=True))

    index_text = (out / "index.json").read_text(encoding="utf-8")
    assert _FAKE_OPENAI_KEY not in index_text
    assert all(_FAKE_OPENAI_KEY not in p.read_text(encoding="utf-8") for p in out.rglob("*.md"))


def test_redact_session_fails_closed_on_deep_structures_and_redacts_keys():
    session = _secret_session("ses-redact-deep")
    deep: dict = {"leaf": _FAKE_OPENAI_KEY}
    for _ in range(10):
        deep = {"n": deep}
    session.messages[0].tool_calls = [{"input": deep, _FAKE_OPENAI_KEY: "v"}]

    redacted, count = redact_session(session)

    dumped = json.dumps(redacted.messages[0].tool_calls)
    assert _FAKE_OPENAI_KEY not in dumped
    assert count >= 2


def test_redact_session_redacts_exported_metadata_fields():
    session = _secret_session("ses-redact-meta")
    session.project_path = f"/work/{_FAKE_OPENAI_KEY}/app"

    redacted, count = redact_session(session)

    assert _FAKE_OPENAI_KEY not in (redacted.project_path or "")
    assert session.project_path == f"/work/{_FAKE_OPENAI_KEY}/app"


def test_structured_tool_data_is_redacted_by_field_name():
    session = _secret_session("ses-redact-field")
    session.messages[0].tool_calls = [
        {"input": {"password": "hunter2!x", "user": "alice", "api_key": "plainvalue99"}}
    ]
    session.messages[0].tool_events = [{"name": "Bash", "token": "abc123def456"}]

    redacted, count = redact_session(session)

    dumped = json.dumps([redacted.messages[0].tool_calls, redacted.messages[0].tool_events])
    assert "hunter2!x" not in dumped
    assert "plainvalue99" not in dumped
    assert "abc123def456" not in dumped
    assert "alice" in dumped
    assert count >= 3


def test_list_values_inherit_the_secret_field_name():
    session = _secret_session("ses-redact-list")
    session.messages[0].tool_calls = [{"input": {"passwords": ["hunter2!x", "second-secret"]}}]
    session.messages[0].tool_calls[0]["input"]["password"] = ["third-secret"]

    redacted, _ = redact_session(session)

    assert "third-secret" not in json.dumps(redacted.messages[0].tool_calls)
