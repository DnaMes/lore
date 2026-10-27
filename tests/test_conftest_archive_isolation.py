"""The conftest autouse fixture must isolate the web modules' import-time paths.

``lore.interfaces.web_data`` binds ``OUTPUT_DIR``/``INDEX_PATH``/
``DELETED_SESSIONS_PATH`` from the real ``~/.lore`` at import time. The
``_isolated_web_archive`` fixture in ``tests/conftest.py`` repoints them at the
per-test pytest tmp directory for every module that imports
``lore.interfaces``.
"""

from pathlib import Path

from lore.interfaces import web, web_data


def test_web_data_output_dir_is_under_pytest_tmp(tmp_path):
    assert isinstance(web_data.OUTPUT_DIR, Path)
    assert web_data.OUTPUT_DIR == tmp_path
    assert "pytest" in web_data.OUTPUT_DIR.parent.parent.name or "pytest" in " ".join(
        web_data.OUTPUT_DIR.parts
    )


def test_web_data_derived_paths_live_in_the_same_tmp_dir(tmp_path):
    assert web_data.INDEX_PATH == tmp_path / "index.json"
    assert web_data.DELETED_SESSIONS_PATH == tmp_path / "deleted_sessions.json"


def test_web_module_paths_match_web_data_paths(tmp_path):
    assert web.OUTPUT_DIR == tmp_path
    assert web.INDEX_PATH == web_data.INDEX_PATH
    assert web.DELETED_SESSIONS_PATH == web_data.DELETED_SESSIONS_PATH
