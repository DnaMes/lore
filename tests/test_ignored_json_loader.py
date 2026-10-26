"""ignored.json loader: failures return an empty set and warn once per process.

Invalid UTF-8, invalid JSON, OS errors and wrong shapes must never raise out of
``load_ignored_session_ids`` (list/search call it), and each failure mode gets
exactly one stderr warning per process, not one per read.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.exporters.index import IndexBuilder
from lore.storage import reader

WARNING_PREFIX = "Warning: could not read ignored.json ("
WARNING_SUFFIX = "); pruned sessions may be visible again.\n"


@pytest.fixture(autouse=True)
def _fresh_warning_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(reader, "_IGNORED_JSON_WARNED", False)


def _write_bytes(tmp_path: Path, payload: bytes) -> Path:
    (tmp_path / "ignored.json").write_bytes(payload)
    return tmp_path


def _warnings(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("Warning:")]


def test_valid_list_returns_ids_and_stays_silent(tmp_path, capsys):
    _write_bytes(tmp_path, json.dumps(["a", "b"]).encode())
    assert reader.load_ignored_session_ids(tmp_path) == {"a", "b"}
    assert capsys.readouterr().err == ""


def test_valid_dict_form_returns_ids(tmp_path, capsys):
    _write_bytes(tmp_path, json.dumps({"session_ids": ["x"]}).encode())
    assert reader.load_ignored_session_ids(tmp_path) == {"x"}
    assert capsys.readouterr().err == ""


def test_missing_file_is_empty_and_silent(tmp_path, capsys):
    assert reader.load_ignored_session_ids(tmp_path) == set()
    assert capsys.readouterr().err == ""


def test_invalid_utf8_returns_empty_with_one_warning(tmp_path, capsys):
    _write_bytes(tmp_path, b'["\xff\xfe"]')
    assert reader.load_ignored_session_ids(tmp_path) == set()
    err = capsys.readouterr().err
    assert len(_warnings(err)) == 1
    assert err.startswith(WARNING_PREFIX)
    assert err.endswith(WARNING_SUFFIX)


def test_invalid_json_returns_empty_with_one_warning(tmp_path, capsys):
    _write_bytes(tmp_path, b'["a", ')
    assert reader.load_ignored_session_ids(tmp_path) == set()
    err = capsys.readouterr().err
    assert len(_warnings(err)) == 1
    assert err.startswith(WARNING_PREFIX)
    assert err.endswith(WARNING_SUFFIX)


def test_directory_in_place_of_file_is_empty_with_warning(tmp_path, capsys):
    (tmp_path / "ignored.json").mkdir()
    assert reader.load_ignored_session_ids(tmp_path) == set()
    assert len(_warnings(capsys.readouterr().err)) == 1


@pytest.mark.parametrize("payload", [b"42", b'"just-a-string"', b'{"session_ids": "abc"}'])
def test_wrong_shape_returns_empty_with_warning(tmp_path, capsys, payload):
    _write_bytes(tmp_path, payload)
    assert reader.load_ignored_session_ids(tmp_path) == set()
    assert len(_warnings(capsys.readouterr().err)) == 1


@pytest.mark.parametrize("payload", [b"{}", b'{"wrong": ["previously-pruned"]}'])
def test_dict_without_session_ids_key_is_shape_error(tmp_path, capsys, payload):
    """An object missing the session_ids key is invalid, not an empty ignore list."""
    _write_bytes(tmp_path, payload)
    assert reader.load_ignored_session_ids(tmp_path) == set()
    assert len(_warnings(capsys.readouterr().err)) == 1


def test_null_is_wrong_shape(tmp_path, capsys):
    _write_bytes(tmp_path, b"null")
    assert reader.load_ignored_session_ids(tmp_path) == set()
    assert len(_warnings(capsys.readouterr().err)) == 1


def test_non_string_entries_are_skipped_with_one_warning(tmp_path, capsys):
    _write_bytes(tmp_path, json.dumps(["ok", 7, None, {"x": 1}, "also-ok"]).encode())
    assert reader.load_ignored_session_ids(tmp_path) == {"ok", "also-ok"}
    err = capsys.readouterr().err
    assert len(_warnings(err)) == 1
    assert "3 non-string" in err


def test_warning_is_emitted_once_per_process(tmp_path, capsys):
    _write_bytes(tmp_path, b"\xff")
    for _ in range(5):
        assert reader.load_ignored_session_ids(tmp_path) == set()
    assert len(_warnings(capsys.readouterr().err)) == 1


def test_warning_once_holds_across_different_failures(tmp_path, capsys):
    _write_bytes(tmp_path, b"\xff")
    reader.load_ignored_session_ids(tmp_path)
    _write_bytes(tmp_path, b"not json")
    reader.load_ignored_session_ids(tmp_path)
    _write_bytes(tmp_path, b"42")
    reader.load_ignored_session_ids(tmp_path)
    assert len(_warnings(capsys.readouterr().err)) == 1


def _session(sid: str) -> UnifiedSession:
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=f"Session {sid}",
        messages=[
            UnifiedMessage(role=Role.USER, content=f"body {sid}", timestamp=datetime(2026, 1, 1))
        ],
    )


def test_list_path_survives_invalid_utf8_ignored_json(tmp_path, capsys):
    """load_index_v2 (list/search) must not raise on undecodable ignored.json."""
    IndexBuilder(tmp_path).build_index([_session("keep"), _session("drop")], {})
    _write_bytes(tmp_path, b'["\xff"]')
    index = reader.load_index_v2(tmp_path)
    assert {s["id"] for s in index["sessions"]} == {"keep", "drop"}
    assert len(_warnings(capsys.readouterr().err)) == 1


def test_valid_file_after_warning_still_loads(tmp_path, capsys):
    _write_bytes(tmp_path, b"\xff")
    assert reader.load_ignored_session_ids(tmp_path) == set()
    _write_bytes(tmp_path, json.dumps(["fixed"]).encode())
    assert reader.load_ignored_session_ids(tmp_path) == {"fixed"}
    assert len(_warnings(capsys.readouterr().err)) == 1


# --- strict loader (used by prune before it rewrites ignored.json) ---


def test_strict_missing_file_is_empty_and_silent(tmp_path, capsys):
    assert reader.load_ignored_session_ids_strict(tmp_path) == set()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "payload, expected", [(b'["a"]', {"a"}), (b'{"session_ids": ["b"]}', {"b"})]
)
def test_strict_accepts_both_valid_forms(tmp_path, payload, expected):
    _write_bytes(tmp_path, payload)
    assert reader.load_ignored_session_ids_strict(tmp_path) == expected


@pytest.mark.parametrize(
    "payload",
    [
        b'{"session_ids": null}',
        b'{"wrong": ["previously-pruned"]}',
        b"{}",
        b"null",
        b'"just-a-string"',
        b"not json {{{",
        b"\xff",
    ],
)
def test_strict_raises_value_error_on_invalid_file(tmp_path, payload):
    _write_bytes(tmp_path, payload)
    with pytest.raises(ValueError):
        reader.load_ignored_session_ids_strict(tmp_path)


def test_strict_mixed_list_keeps_valid_ids(tmp_path, capsys):
    """Non-string entries are skipped (warning), valid ids are preserved."""
    _write_bytes(tmp_path, json.dumps(["old-pruned", 7]).encode())
    assert reader.load_ignored_session_ids_strict(tmp_path) == {"old-pruned"}
    assert len(_warnings(capsys.readouterr().err)) == 1
