"""Synthetic VS2022 MessagePack streams, never private source transcripts."""

import json
import os
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import msgpack
import pytest

from lore.core.models import Role, Tool
from lore.extractors.factory import get_all_extractors
from lore.extractors.visual_studio import MAX_SOURCE_BYTES, VisualStudioCopilotExtractor
from lore.interfaces.web_helpers import get_style
from lore.utils.home_discovery import _discover_cached
from lore.utils.security import validate_session_id, validate_tool_name
from lore.utils.tooling import is_supported_tool, normalize_tool_name

CREATED = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("LORE_EXTRA_HOME_PATHS", raising=False)
    monkeypatch.delenv("LORE_VISUAL_STUDIO_PROJECT_PATHS", raising=False)
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "1")
    _discover_cached.cache_clear()
    yield home
    _discover_cached.cache_clear()


def _stream(
    project, records=None, *, name="Synthetic session", version=1, owner="owner", file_id=None
):
    path = (
        project / ".vs" / "Solution" / "copilot-chat" / owner / "sessions" / str(file_id or uuid4())
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"Name": name, "TimeCreated": msgpack.Timestamp.from_datetime(CREATED)}
    if records is None:
        records = [[0, {"Content": [[3, {"Content": "Question"}]]}]]
    path.write_bytes(
        b"".join(msgpack.packb(x, use_bin_type=True) for x in [version, header, *records])
    )
    return path


def test_stream_content_and_tool_envelopes(environment):
    project = environment / "source" / "repos" / "Example"
    path = _stream(
        project,
        [
            [
                0,
                {
                    "MessageId": "user-id",
                    "Content": [[3, {"Content": "Question"}]],
                    "Context": {"Content": "private attachment"},
                    "Functions": [{"Name": "not_called"}],
                },
            ],
            [
                1,
                {
                    "MessageId": "assistant-id",
                    "Model": [0, {"Id": "model-id"}],
                    "Content": [
                        [0, {"Content": "Before"}],
                        [1, {"Content": "print('hello')", "LanguageMarkdownIdentifier": "python"}],
                        [3, {"Content": "After"}],
                        [10, {"Content": "Reasoning", "EncryptedContent": b"ignored"}],
                        [
                            7,
                            {
                                "Id": b"\x01\x02",
                                "State": "Completed",
                                "Status": 1,
                                "Function": {
                                    "Name": "read_file",
                                    "Arguments": [0, {"json": '{"path":"demo.py"}'}],
                                },
                                "Result": [
                                    0,
                                    {
                                        "Value": {
                                            "ValueContainer": [
                                                "string",
                                                msgpack.packb("result"),
                                                False,
                                            ]
                                        }
                                    },
                                ],
                            },
                        ],
                        [99, {"Content": "unknown content must not leak"}],
                    ],
                },
            ],
            [42, {"Content": "unknown record"}],
        ],
    )
    os.utime(path, (CREATED.timestamp() - 60, CREATED.timestamp() - 60))
    extractor = VisualStudioCopilotExtractor()
    (session,) = extractor.extract_sessions()
    assert session.tool is Tool.VISUAL_STUDIO_COPILOT
    assert session.source_path == str(path)
    assert session.project_path == str(project)
    assert session.created_at == session.last_updated == CREATED
    assert session.title == "Synthetic session"
    assert session.source_version == "1"
    assert session.parser_version == extractor.PARSER_VERSION
    assert validate_session_id(session.session_id)
    user, assistant = session.messages
    assert [user.role, assistant.role] == [Role.USER, Role.ASSISTANT]
    assert user.content == "Question" and not user.tool_calls
    assert assistant.content == "Before\n\n```python\nprint('hello')\n```\n\nAfter"
    assert assistant.reasoning == "Reasoning"
    assert assistant.model == "model-id"
    assert assistant.timestamp == CREATED
    assert assistant.extensions["visual_studio"]["timestamp_inferred"] is True
    (call,) = assistant.tool_calls
    assert call["tool"] == "read_file" and call["input"] == '{"path":"demo.py"}'
    assert call["id"] == "0102" and call["status"] == "Completed"
    assert call["output"] == "result"
    assert extractor.discovery == {"files_matched": 1, "files_parsed": 1, "records_unknown": 2}
    json.dumps(call)


def test_profile_roots_extra_projects_cwd_and_duplicates(environment, tmp_path, monkeypatch):
    mounted = tmp_path / "Windows" / "Users" / "User"
    project = mounted / "source" / "repos" / "Mounted"
    local = environment / "source" / "repos" / "Local"
    external = tmp_path / "external"
    shared_id = uuid4()
    paths = {
        _stream(project, file_id=shared_id),
        _stream(project, file_id=shared_id, owner="other-owner"),
        _stream(local, file_id=shared_id),
        _stream(external),
        _stream(Path.cwd()),
    }
    monkeypatch.setenv("LORE_EXTRA_HOME_PATHS", os.pathsep.join([str(mounted), str(mounted)]))
    monkeypatch.setenv(
        "LORE_VISUAL_STUDIO_PROJECT_PATHS", os.pathsep.join([str(project), str(external)])
    )
    extractor = VisualStudioCopilotExtractor()
    assert extractor.is_available()
    sessions = list(extractor.extract_sessions())
    assert {s.source_path for s in sessions} == {str(p) for p in paths}
    assert len({s.session_id for s in sessions}) == len(paths)
    assert extractor.discovery["files_matched"] == len(paths)
    from lore.watcher import collect_extractor_paths

    assert set(collect_extractor_paths([extractor])) == {p.parent for p in paths}


@pytest.mark.parametrize("damage", ["version", "truncated", "garbage", "header", "content"])
def test_bad_sources_isolated(environment, caplog, damage):
    project = environment / "source" / "repos" / "Example"
    valid = _stream(project)
    bad = _stream(project, version=2 if damage == "version" else 1)
    if damage == "truncated":
        bad.write_bytes(bad.read_bytes() + msgpack.packb([1, {"Content": []}])[:-1])
    elif damage == "garbage":
        bad.write_bytes(b"\xc1")
    elif damage == "header":
        bad.write_bytes(msgpack.packb(1) + msgpack.packb({"Name": "bad"}))
    elif damage == "content":
        bad = _stream(project, [[0, {"Content": "not an array"}]], file_id=bad.name)
    sessions = list(VisualStudioCopilotExtractor().extract_sessions())
    assert [s.source_path for s in sessions] == [str(valid)]
    assert str(bad) in caplog.text


def test_oversize_not_opened_and_unchanged_not_read(environment, monkeypatch, caplog):
    project = environment / "source" / "repos" / "Example"
    huge = _stream(project)
    with huge.open("r+b") as source:
        source.truncate(MAX_SOURCE_BYTES + 1)
    unchanged = _stream(project)
    extractor = VisualStudioCopilotExtractor()
    extractor.known_source_mtimes = {str(unchanged): unchanged.stat().st_mtime_ns}
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert path not in (huge, unchanged), "source should never be opened"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    assert list(extractor.extract_sessions()) == []
    assert extractor.skipped_sources == {str(unchanged)}
    assert "64 MiB" in caplog.text


def test_policy_fallback_title_mtime_and_incremental_version(environment, monkeypatch):
    path = _stream(environment / "source" / "repos" / "Example", name=None)
    extractor = VisualStudioCopilotExtractor()
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "3")
    assert list(extractor.extract_sessions()) == []
    assert extractor.skip_counts["too_few_user_prompts"] == 1
    monkeypatch.setenv("LORE_MIN_USER_PROMPTS", "1")
    extractor.known_source_mtimes = {str(path): path.stat().st_mtime_ns}
    extractor.known_source_parser_versions = {str(path): 0}
    (session,) = extractor.extract_sessions()
    assert session.title == "Question"
    assert session.last_updated == datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def test_registration_and_aliases(environment):
    assert Tool.VISUAL_STUDIO_COPILOT in {e.tool for e in get_all_extractors()}
    for alias in ("visual-studio-copilot", "visual-studio", "vs2022"):
        assert normalize_tool_name(alias) == "visual-studio-copilot"
        assert is_supported_tool(alias) and validate_tool_name(alias)
    assert get_style("visual-studio-copilot")["name"] == "Visual Studio Copilot"
    assert not validate_session_id("visual-studio:../../etc/passwd")


def test_unavailable_root_and_unknown_tool_result(environment, tmp_path, monkeypatch, caplog):
    missing = tmp_path / "missing"
    monkeypatch.setenv("LORE_VISUAL_STUDIO_PROJECT_PATHS", str(missing))
    extractor = VisualStudioCopilotExtractor()
    assert not extractor.is_available()
    assert str(missing) in caplog.text
    unknown = [4, {"FutureResult": b"\x01\x02"}]
    assert extractor._tool_output(unknown) == [4, {"FutureResult": "0102"}]
    assert extractor.discovery["records_unknown"] == 1
    result = [0, {"Value": {"ValueContainer": ["object", msgpack.packb({"ok": [1, 2]}), False]}}]
    assert extractor._tool_output(result) == {"ok": [1, 2]}


def test_full_cli_export(environment, tmp_path, monkeypatch):
    import lore_cli

    source = _stream(environment / "source" / "repos" / "Example")
    before = source.read_bytes(), source.stat().st_mtime_ns
    monkeypatch.setattr(lore_cli, "get_all_extractors", lambda: [VisualStudioCopilotExtractor()])
    output = tmp_path / "export"
    lore_cli.cmd_export(
        Namespace(output_dir=str(output), tool="visual-studio-copilot", project=None)
    )
    entries = json.loads((output / "index.json").read_text())["sessions"]
    (entry,) = entries
    assert entry["tool"] == "visual-studio-copilot"
    assert entry["id"].startswith("visual-studio:")
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    (exported,) = output.rglob("*.md")
    assert "Question" in exported.read_text()
