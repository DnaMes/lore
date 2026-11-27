from annuli.extractors.opencode import OpenCodeExtractor


def test_opencode_discovers_dot_opencode_storage(monkeypatch, tmp_path):
    home = tmp_path / "home"
    local_storage = home / ".local" / "share" / "opencode" / "storage"
    dot_storage = home / ".opencode" / "storage"

    for root in [local_storage, dot_storage]:
        (root / "session").mkdir(parents=True, exist_ok=True)
        (root / "message").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))

    extractor = OpenCodeExtractor(force_full=True)
    roots = {str(path) for path in extractor.storage_roots}

    assert str(local_storage.resolve()) in roots
    assert str(dot_storage.resolve()) in roots


def test_opencode_discovers_nested_home_subfolder_storage(monkeypatch, tmp_path):
    home = tmp_path / "home"
    nested_storage = home / "projects" / "sample" / ".opencode" / "storage"
    (nested_storage / "session").mkdir(parents=True, exist_ok=True)
    (nested_storage / "message").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OPENCODE_HOME_SCAN_DEPTH", "5")
    monkeypatch.setenv("OPENCODE_HOME_SCAN_LIMIT", "8")

    extractor = OpenCodeExtractor(force_full=True)
    roots = {str(path) for path in extractor.storage_roots}

    assert str(nested_storage.resolve()) in roots
