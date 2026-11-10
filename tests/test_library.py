"""Browsable on-disk library from the v2 store (``lore library``)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lore.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from lore.library.builder import _fingerprint, build_library
from lore.services.index import load_deleted_session_ids, save_deleted_session_ids
from lore.storage import retention
from lore.storage.reader import load_session_messages_v2
from lore.storage.schema import initialise
from lore.storage.summaries import save_summary, summary_fingerprint
from lore.storage.tags import add_session_tag
from lore.storage.writer import v2_db_path, write_sessions

T0 = datetime(2026, 11, 8, 10, 0, 0, tzinfo=timezone.utc)
PROJECT = "/home/user/alpha"
PROJECT_B = "/home/user/beta"


def _id_tail(session_id: str) -> str:
    """The id part of a library session file name (see builder._session_stem)."""
    return re.sub(r"[^A-Za-z0-9]", "", session_id)[-12:]


def _msg(role: Role, content: str, minutes: int = 0) -> UnifiedMessage:
    return UnifiedMessage(role=role, content=content, timestamp=T0 + timedelta(minutes=minutes))


def _session(
    sid: str,
    *,
    title: str = "Alpha work",
    origin: str = "interactive",
    project: str = PROJECT,
    day_offset: int = 0,
) -> UnifiedSession:
    created = T0 + timedelta(days=day_offset)
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=created,
        last_updated=created + timedelta(hours=1),
        project_path=project,
        title=title,
        origin=origin,
        messages=[
            _msg(Role.USER, f"prompt for {sid}", 0),
            _msg(Role.ASSISTANT, f"answer for {sid}", 1),
        ],
    )


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


def _all_wikilinks(library_dir: Path) -> set[str]:
    links: set[str] = set()
    for path in library_dir.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for match in _WIKILINK_RE.finditer(text):
            links.add(match.group(1).strip())
    return links


def _library_links(links: set[str]) -> set[str]:
    prefixes = ("Sessions/", "Projects/", "Days/")
    return {link for link in links if link.startswith(prefixes)}


def _wikilink_targets_exist(library_dir: Path, links: set[str]) -> list[str]:
    missing: list[str] = []
    for link in _library_links(links):
        candidate = library_dir / f"{link}.md"
        if not candidate.is_file():
            missing.append(link)
    return missing


@pytest.fixture()
def store_and_library(tmp_path: Path) -> tuple[Path, Path]:
    store = tmp_path / "store"
    store.mkdir()
    library = tmp_path / "library"
    initialise(v2_db_path(store))
    return store, library


def _seed_store(store: Path) -> list[UnifiedSession]:
    sessions = [
        _session("human-interactive", title="Ship library"),
        _session("human-second", title="Refine layout", day_offset=1),
        _session("auto-exec", title="Cron batch", origin="exec"),
        _session(
            "unsafe-title",
            title="bad[[title]]|slash\\ ..",
            project="/tmp/evil/../../../etc",
        ),
    ]
    write_sessions(v2_db_path(store), sessions)
    add_session_tag(store, "human-interactive", "library")
    save_summary(
        store,
        level="session",
        scope_key="session:human-interactive",
        session_id="human-interactive",
        fingerprint=summary_fingerprint("body", "test", 1, 1),
        model="test",
        prompt_version=1,
        compressor_version=1,
        payload={"one_liner": "Built the vault export", "goal": {"text": "g", "cites": []}},
    )
    return sessions


def test_layout_and_wikilinks_resolve(store_and_library: tuple[Path, Path]) -> None:
    store, library = store_and_library
    _seed_store(store)
    stats = build_library(store, library, full=True)
    assert stats.sessions_written >= 3
    assert (library / "Index.md").is_file()
    assert list((library / "Projects").glob("*.md"))
    assert list((library / "Days").glob("*.md"))
    assert list((library / "Sessions").rglob("*.md"))
    links = _all_wikilinks(library)
    assert links
    missing = _wikilink_targets_exist(library, links)
    assert missing == [], f"missing wikilink targets: {missing}"


def test_automated_sessions_excluded(store_and_library: tuple[Path, Path]) -> None:
    store, library = store_and_library
    _seed_store(store)
    build_library(store, library, full=True)
    text = "\n".join(p.read_text(encoding="utf-8") for p in library.rglob("*.md"))
    assert "auto-exec" not in text
    assert "Cron batch" not in text


def test_incremental_rewrites_only_touched_pages(store_and_library: tuple[Path, Path]) -> None:
    store, library = store_and_library
    _seed_store(store)
    first = build_library(store, library, full=True)
    assert first.sessions_written >= 3
    before = {p: p.stat().st_mtime_ns for p in library.rglob("*.md")}
    add_session_tag(store, "human-interactive", "incremental")
    second = build_library(store, library, full=False)
    assert second.sessions_written == 1
    assert second.projects_written >= 1
    assert second.days_written >= 1
    assert second.skipped >= 2
    changed = [p for p in library.rglob("*.md") if p.stat().st_mtime_ns != before.get(p, 0)]
    assert (library / "Index.md") in changed
    assert any("human-interactive" in p.name or "Ship-library" in p.name for p in changed)


def test_pruned_session_gets_no_library_page(store_and_library: tuple[Path, Path]) -> None:
    store, library = store_and_library
    sessions = [_session("keep-me", title="Visible"), _session("pruned-me", title="Pruned")]
    write_sessions(v2_db_path(store), sessions)
    retention.record_ignored(store, ["pruned-me"], reason="prune")
    (store / "ignored.json").write_text('["pruned-me"]', encoding="utf-8")
    build_library(store, library, full=True)
    text = "\n".join(p.read_text(encoding="utf-8") for p in library.rglob("*.md"))
    assert "pruned-me" not in text
    assert "Pruned" not in text
    assert not list((library / "Sessions").rglob("*pruned*"))
    manifest = json.loads((library / ".lore-library.json").read_text(encoding="utf-8"))
    assert "pruned-me" not in manifest.get("sessions", {})


def test_project_scoped_build_preserves_other_project(
    store_and_library: tuple[Path, Path],
) -> None:
    store, library = store_and_library
    sessions = [
        _session("alpha-one", title="Alpha one"),
        _session("beta-one", title="Beta one", project=PROJECT_B),
    ]
    write_sessions(v2_db_path(store), sessions)
    build_library(store, library, full=True)
    beta_pages = list((library / "Sessions").rglob("*Beta-one*"))
    assert beta_pages
    manifest_before = json.loads((library / ".lore-library.json").read_text(encoding="utf-8"))
    assert "beta-one" in manifest_before["sessions"]

    build_library(store, library, full=False, project=PROJECT)
    assert list((library / "Sessions").rglob("*Beta-one*"))
    manifest_after = json.loads((library / ".lore-library.json").read_text(encoding="utf-8"))
    assert manifest_after["sessions"]["beta-one"] == manifest_before["sessions"]["beta-one"]


def test_project_scoped_build_removes_deleted_session_in_scope(
    store_and_library: tuple[Path, Path],
) -> None:
    store, library = store_and_library
    sessions = [
        _session("alpha-drop", title="Alpha drop"),
        _session("beta-keep", title="Beta keep", project=PROJECT_B),
    ]
    write_sessions(v2_db_path(store), sessions)
    build_library(store, library, full=True)
    assert list((library / "Sessions").rglob("*Alpha-drop*"))

    deleted_path = store / "deleted_sessions.json"
    save_deleted_session_ids({"alpha-drop"}, store, deleted_path)
    build_library(store, library, full=False, project=PROJECT)
    assert not list((library / "Sessions").rglob("*Alpha-drop*"))
    assert list((library / "Sessions").rglob("*Beta-keep*"))
    manifest = json.loads((library / ".lore-library.json").read_text(encoding="utf-8"))
    assert "alpha-drop" not in manifest["sessions"]
    assert "beta-keep" in manifest["sessions"]


def test_removed_session_page_deleted(store_and_library: tuple[Path, Path]) -> None:
    store, library = store_and_library
    _seed_store(store)
    build_library(store, library, full=True)
    paths_before = list((library / "Sessions").rglob(f"*{_id_tail('human-second')}*.md"))
    assert paths_before
    deleted_path = store / "deleted_sessions.json"
    save_deleted_session_ids({"human-second"}, store, deleted_path)
    assert "human-second" in load_deleted_session_ids(deleted_path)
    stats = build_library(store, library, full=False)
    assert not list((library / "Sessions").rglob(f"*{_id_tail('human-second')}*.md"))
    assert stats.sessions_written == 0


def test_unsafe_titles_slugged_and_escaped(store_and_library: tuple[Path, Path]) -> None:
    store, library = store_and_library
    _seed_store(store)
    build_library(store, library, full=True)
    session_files = list((library / "Sessions").rglob(f"*{_id_tail('unsafe-title')}*.md"))
    assert session_files
    name = session_files[0].name
    assert "/" not in name and ".." not in name
    body = session_files[0].read_text(encoding="utf-8")
    assert "\\|" in body or "bad" in body


def test_output_never_escapes_library_root(store_and_library: tuple[Path, Path]) -> None:
    store, library = store_and_library
    _seed_store(store)
    build_library(store, library, full=True)
    library_resolved = library.resolve()
    for path in library.rglob("*"):
        assert path.resolve().is_relative_to(library_resolved)


def test_subagent_composite_id_excluded_without_origin(
    store_and_library: tuple[Path, Path],
) -> None:
    store, library = store_and_library
    sessions = [
        _session("human-main", title="Kickzo ships"),
        _session(
            "parent-session:agent-deadbeef",
            title="[subagent agent-deadbeef]",
            origin="unknown",
        ),
    ]
    write_sessions(v2_db_path(store), sessions)
    build_library(store, library, full=True)
    text = "\n".join(p.read_text(encoding="utf-8") for p in library.rglob("*.md"))
    assert "parent-session:agent-deadbeef" not in text
    assert "[subagent agent-deadbeef]" not in text
    assert "Kickzo ships" in text or "prompt for human-main" in text


def test_display_title_fallback_and_command_name(
    store_and_library: tuple[Path, Path],
) -> None:
    store, library = store_and_library
    sessions = [
        UnifiedSession(
            tool=Tool.CLAUDE_CODE,
            session_id="skill-fallback01",
            created_at=T0,
            last_updated=T0 + timedelta(hours=1),
            project_path=PROJECT,
            title='<skill name="using-superpowers" path="...">',
            origin="interactive",
            messages=[
                _msg(
                    Role.USER,
                    "Base directory for this skill: /home/dnames/.agents/skills/using-superpowers",
                    0,
                ),
                _msg(Role.USER, "Wire library titles like the web UI", 1),
                _msg(Role.ASSISTANT, "ok", 2),
            ],
        ),
        UnifiedSession(
            tool=Tool.CLAUDE_CODE,
            session_id="cmd-title00001",
            created_at=T0 + timedelta(hours=2),
            last_updated=T0 + timedelta(hours=3),
            project_path=PROJECT,
            title="using-superpowers /using-sup...",
            origin="interactive",
            messages=[
                _msg(
                    Role.USER,
                    "<command-name>/loop</command-name><command-args>5m</command-args>",
                    0,
                ),
                _msg(Role.ASSISTANT, "looping", 1),
            ],
        ),
    ]
    write_sessions(v2_db_path(store), sessions)
    build_library(store, library, full=True)
    project = next(library.glob("Projects/*.md")).read_text(encoding="utf-8")
    assert "Wire library titles like the web UI" in project
    assert "/loop 5m" in project
    assert '<skill name="using-superpowers"' not in project
    assert "using-superpowers /using-sup" not in project


def test_scratch_shelf_and_short_project_label(
    store_and_library: tuple[Path, Path],
) -> None:
    store, library = store_and_library
    sessions = [
        _session("kickzo-human", project="/home/user/projects/games/kickzo"),
        _session("tmp-scratch", project="/tmp/scratch-run"),
    ]
    write_sessions(v2_db_path(store), sessions)
    build_library(store, library, full=True)
    index = (library / "Index.md").read_text(encoding="utf-8")
    kickzo_page = None
    for path in library.glob("Projects/*.md"):
        body = path.read_text(encoding="utf-8")
        if "games/kickzo" in body and "/home/user/projects/games/kickzo" in body:
            kickzo_page = body
    assert kickzo_page is not None
    assert kickzo_page.startswith("---\n")
    assert "project:" in kickzo_page.split("---")[1]
    assert "# games/kickzo" in kickzo_page
    projects_section = index.split("## Projects", 1)[1].split("##", 1)[0]
    scratch_section = index.split("## Scratch & unknown", 1)[1].split("##", 1)[0]
    assert "games/kickzo" in projects_section
    assert "/tmp/scratch-run" not in projects_section.split("games/kickzo")[0]
    assert "scratch-run" in scratch_section or "tmp/scratch-run" in scratch_section
    assert projects_section.index("games/kickzo") < index.index("## Scratch & unknown")


def test_counts_only_sessions_with_library_pages(
    store_and_library: tuple[Path, Path],
) -> None:
    store, library = store_and_library
    sessions = [
        _session("with-body", title="Has transcript"),
        UnifiedSession(
            tool=Tool.CLAUDE_CODE,
            session_id="no-body00001",
            created_at=T0,
            last_updated=T0 + timedelta(hours=1),
            project_path=PROJECT,
            title="Index only",
            origin="interactive",
            messages=[],
        ),
    ]
    write_sessions(v2_db_path(store), sessions)
    build_library(store, library, full=True)
    index = (library / "Index.md").read_text(encoding="utf-8")
    project = next(library.glob("Projects/*.md")).read_text(encoding="utf-8")
    assert "1 sessions" in index or "1 session" in index
    assert "**1** sessions with library pages" in project
    assert "Index only" not in project
    assert not list((library / "Sessions").rglob("*no-body*"))


def test_session_page_uses_display_title_not_raw_meta(
    store_and_library: tuple[Path, Path],
) -> None:
    store, library = store_and_library
    sessions = [
        UnifiedSession(
            tool=Tool.CLAUDE_CODE,
            session_id="display-head01",
            created_at=T0,
            last_updated=T0 + timedelta(hours=1),
            project_path=PROJECT,
            title='<skill name="using-superpowers" path="...">',
            origin="interactive",
            messages=[
                _msg(Role.USER, "Real topic for session page heading", 0),
                _msg(Role.ASSISTANT, "ok", 1),
            ],
        ),
    ]
    write_sessions(v2_db_path(store), sessions)
    build_library(store, library, full=True)
    page = next(library.glob("Sessions/**/*.md"))
    body = page.read_text(encoding="utf-8")
    assert "# Real topic for session page heading" in body
    assert "title: Real topic for session page heading" in body
    assert '<skill name="using-superpowers"' not in body


def test_stale_session_path_removed_when_title_or_project_changes(
    store_and_library: tuple[Path, Path],
) -> None:
    store, library = store_and_library
    sid = "rename-session"
    write_sessions(
        v2_db_path(store),
        [_session(sid, title="First title", project=PROJECT)],
    )
    build_library(store, library, full=True)
    old_pages = list((library / "Sessions").rglob(f"*{_id_tail(sid)}*.md"))
    assert len(old_pages) == 1
    old_path = old_pages[0]

    write_sessions(
        v2_db_path(store),
        [_session(sid, title="Renamed title", project=PROJECT_B)],
    )
    build_library(store, library, full=False)
    assert not old_path.is_file()
    new_pages = list((library / "Sessions").rglob(f"*{_id_tail(sid)}*.md"))
    assert len(new_pages) == 1
    assert new_pages[0] != old_path


def test_incremental_skips_message_load_for_unchanged_sessions(
    store_and_library: tuple[Path, Path], monkeypatch
) -> None:
    store, library = store_and_library
    _seed_store(store)
    build_library(store, library, full=True)
    calls: list[str] = []
    real = load_session_messages_v2

    def _track(output_dir: Path, session_id: str, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(session_id)
        return real(output_dir, session_id, **kwargs)

    monkeypatch.setattr("lore.library.builder.load_session_messages_v2", _track)
    add_session_tag(store, "human-interactive", "another-tag")
    calls.clear()
    build_library(store, library, full=False)
    assert "human-second" not in calls
    assert "human-interactive" in calls


def test_fingerprint_includes_title_and_prompt_outline(
    store_and_library: tuple[Path, Path],
) -> None:
    base_meta = {
        "id": "fp-sess",
        "updated": "2026-11-08T10:00:00+00:00",
        "messages": 2,
        "title": "Alpha",
        "prompt_outline": "outline a",
    }
    tags: list[str] = []
    fp_a = _fingerprint(base_meta, tags, None)
    fp_b = _fingerprint({**base_meta, "title": "Beta"}, tags, None)
    fp_c = _fingerprint({**base_meta, "prompt_outline": "outline b"}, tags, None)
    assert fp_a != fp_b
    assert fp_a != fp_c

    store, library = store_and_library
    sid = "title-backfill"
    sess = _session(sid, title="Stored title v1")
    write_sessions(v2_db_path(store), [sess])
    build_library(store, library, full=True)
    old_page = next((library / "Sessions").rglob(f"*{_id_tail(sid)}*.md"))
    assert "Stored title v1" in old_page.read_text(encoding="utf-8")

    sess2 = _session(sid, title="Stored title v2")
    sess2.last_updated = sess.last_updated
    write_sessions(v2_db_path(store), [sess2])
    stats = build_library(store, library, full=False)
    assert stats.sessions_written == 1
    assert not old_page.is_file()
    new_page = next((library / "Sessions").rglob(f"*{_id_tail(sid)}*.md"))
    assert "Stored title v2" in new_page.read_text(encoding="utf-8")


def test_cli_library_prints_stats(
    store_and_library: tuple[Path, Path], monkeypatch, capsys
) -> None:
    import sys

    store, library = store_and_library
    _seed_store(store)
    import lore_cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lore",
            "--output-dir",
            str(store),
            "library",
            "--output",
            str(library),
            "--full",
        ],
    )
    lore_cli.main()
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["sessions_written"] >= 3


def test_session_paths_do_not_collide_on_a_shared_id_prefix():
    """Time-ordered ids (OpenCode ``ses_…``, UUIDv7) share their first characters;
    two such sessions with the same day and title must still get two pages."""
    from lore.library.builder import _session_stem

    base = {"title": "One word reply", "created": "2026-06-24T10:00:00", "project": "/p"}
    _, first = _session_stem({**base, "id": "ses_1046bec12ffeYABmVqhiLqOC7l"})
    _, second = _session_stem({**base, "id": "ses_1046da9ccffeDqIpViSo5fQXny"})
    assert first != second
