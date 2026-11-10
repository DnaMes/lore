"""Export paths recorded by a host sync must resolve inside a container mount.

The host `lore-sync` unit writes absolute host paths (``/home/<user>/.lore/projects/...``)
into the index; the web container mounts the same directory elsewhere
(``/home/ai/.lore``). Before the fix no export resolved there, so every session
page fell back to live extraction or a metadata-only card.
"""

from pathlib import Path

from lore.interfaces import web_data


def _container_output(monkeypatch, tmp_path) -> Path:
    output = tmp_path / "container" / ".lore"
    export = output / "projects" / "p" / "claude-code" / "s.md"
    export.parent.mkdir(parents=True)
    export.write_text("session_id: s\n", encoding="utf-8")
    monkeypatch.setattr(web_data, "OUTPUT_DIR", output)
    return export


def test_host_path_is_rebased_onto_the_output_dir(monkeypatch, tmp_path):
    export = _container_output(monkeypatch, tmp_path)
    host_path = "/home/someone/.lore/projects/p/claude-code/s.md"
    assert web_data.resolve_export_path(host_path) == export.resolve()


def test_path_inside_the_output_dir_still_resolves(monkeypatch, tmp_path):
    export = _container_output(monkeypatch, tmp_path)
    assert web_data.resolve_export_path(str(export)) == export.resolve()


def test_rebase_cannot_escape_the_output_dir(monkeypatch, tmp_path):
    _container_output(monkeypatch, tmp_path)
    outside = tmp_path / "secret.md"
    outside.write_text("x", encoding="utf-8")
    escaping = f"/home/someone/.lore/../../../../{outside}"
    assert web_data.resolve_export_path(escaping) is None
    assert web_data.resolve_export_path("/home/someone/.lore/projects/p/missing.md") is None
    assert web_data.resolve_export_path("/home/someone/other/projects/p/claude-code/s.md") is None
