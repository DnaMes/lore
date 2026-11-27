import hashlib
import os

from annuli.extractors.gemini import GeminiCLIExtractor
from annuli.extractors.opencode import OpenCodeExtractor
from annuli.utils.security import sanitize_path


def test_sanitize_path_allows_absolute_when_enabled(tmp_path):
    base = tmp_path / "base"
    base.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir(parents=True)

    resolved = sanitize_path(str(target), base, allow_absolute=True)

    assert resolved == target.resolve()


def test_sanitize_path_rejects_relative_traversal(tmp_path):
    base = tmp_path / "base"
    base.mkdir(parents=True)

    try:
        sanitize_path("../escape", base)
    except ValueError as exc:
        assert "Path traversal" in str(exc)
    else:
        raise AssertionError("expected ValueError for traversal path")


def test_sanitize_path_rejects_absolute_traversal_when_allowed(tmp_path):
    base = tmp_path / "base"
    base.mkdir(parents=True)
    unsafe = f"{tmp_path}/base/../outside"

    try:
        sanitize_path(unsafe, base, allow_absolute=True)
    except ValueError as exc:
        assert "Path traversal" in str(exc)
    else:
        raise AssertionError("expected ValueError for absolute traversal path")


def test_gemini_project_roots_ignores_unsafe_relative_paths(monkeypatch, tmp_path):
    home = tmp_path / "home"
    safe_root = home / "projects"
    safe_root.mkdir(parents=True)
    (home / ".gemini" / "tmp").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "GEMINI_PROJECT_ROOTS",
        os.pathsep.join(["../outside", str(safe_root)]),
    )

    extractor = GeminiCLIExtractor()

    safe_hash = hashlib.sha256(str(safe_root.resolve()).encode()).hexdigest()
    unsafe_hash = hashlib.sha256(str((home / ".." / "outside").resolve()).encode()).hexdigest()

    assert safe_hash in extractor._hash_to_path
    assert unsafe_hash not in extractor._hash_to_path


def test_opencode_env_paths_ignore_unsafe_relative_paths(monkeypatch, tmp_path):
    home = tmp_path / "home"
    db_path = home / "custom" / "opencode.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_text("", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(
        "OPENCODE_SQLITE_PATHS",
        os.pathsep.join(["../outside.db", str(db_path)]),
    )

    extractor = OpenCodeExtractor(force_full=True)

    sqlite_paths = {path.resolve() for path in extractor.sqlite_db_paths}
    assert db_path.resolve() in sqlite_paths
    assert (home / ".." / "outside.db").resolve() not in sqlite_paths
