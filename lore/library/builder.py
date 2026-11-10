"""Build a browsable Markdown library from the v2 archive store."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from lore.core.models import UnifiedSession
from lore.exporters.index import IndexBuilder, is_low_quality_title
from lore.exporters.markdown import MarkdownExporter
from lore.interfaces.web_data import _tool_from_value
from lore.services.index import annotate_display_titles, is_automated, load_deleted_session_ids
from lore.services.memory_context import select_context_memories
from lore.storage.memory import MemoryEntry, list_memory
from lore.storage.reader import load_ignored_session_ids, load_index_v2, load_session_messages_v2
from lore.storage.summaries import get_summary
from lore.storage.tags import all_tags_by_session, list_all_tags
from lore.storage.writer import v2_db_path
from lore.utils.datetime import make_naive
from lore.utils.injected import classify_injected_display
from lore.utils.paths import project_to_dirname, sanitize_filename

_MANIFEST = ".lore-library.json"
_SESSIONS = "Sessions"
_PROJECTS = "Projects"
_DAYS = "Days"
_MAX_SLUG = 80
_WIKILINK_ESCAPE = str.maketrans({"\\": "\\\\", "[": "\\[", "]": "\\]", "|": "\\|"})
_SCRATCH_SHELF = "Scratch & unknown"
_DEFAULT_INJECTED_RULES = {"session_classify_injected_events": True}


@dataclass
class LibraryStats:
    sessions_written: int = 0
    projects_written: int = 0
    days_written: int = 0
    skipped: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)


@dataclass
class _SessionEntry:
    meta: Dict[str, Any]
    tags: List[str]
    summary: Optional[str]
    fingerprint: str
    project_slug: str
    day: str
    stem: str
    rel_link: str
    has_body: bool
    display_title: str = ""
    published: bool = False


def _library_skip_session(meta: Dict[str, Any]) -> bool:
    """Same automated hiding as the web session list, plus composite subagent ids."""
    if is_automated(meta):
        return True
    sid = str(meta.get("id") or "")
    if ":agent-" in sid:
        return True
    if meta.get("relation") == "subagent":
        return True
    return False


def _library_project_label(project_path: Optional[str]) -> str:
    if not project_path:
        return "_unknown"
    parts = [p for p in Path(project_path).parts if p not in ("", "/")]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    if parts:
        return parts[-1]
    return project_path


def _is_scratch_shelf_project(project_path: str) -> bool:
    raw = (project_path or "").strip()
    if not raw:
        return True
    normalized = raw.replace("\\", "/").rstrip("/")
    if normalized == "_unknown" or project_to_dirname(raw).strip("-") in ("", "_unknown"):
        return True
    expanded = str(Path(raw).expanduser())
    return expanded == "/tmp" or expanded.startswith("/tmp/")


def _first_real_user_prompt(
    builder: IndexBuilder,
    messages: Iterable[Any],
    *,
    tool: str = "",
) -> str:
    for msg in messages:
        role = getattr(msg, "role", None)
        role_s = str(getattr(role, "value", role) or "")
        if role_s != "user":
            continue
        text = (getattr(msg, "content", None) or "").strip()
        if not text:
            continue
        kind = classify_injected_display(tool, role_s, text, _DEFAULT_INJECTED_RULES)
        if kind.kind == "event":
            continue
        if kind.kind == "slash_prompt" and kind.prompt_title:
            return kind.prompt_title[:140]
        cleaned = builder._clean_title_text(text)
        if not cleaned or is_low_quality_title(cleaned):
            continue
        return cleaned[:140]
    return ""


def _library_native_title_usable(text: str) -> bool:
    return bool(text) and not is_low_quality_title(text)


def _library_base_title(
    meta: Dict[str, Any],
    messages: Iterable[Any],
    builder: IndexBuilder,
) -> str:
    session_id = str(meta.get("id") or "")
    id8 = session_id[:8] if session_id else "unknown"
    native = (meta.get("title") or "").strip()
    cleaned_native = builder._clean_title_text(native)
    if _library_native_title_usable(cleaned_native):
        return cleaned_native

    outline = (meta.get("prompt_outline") or "").strip()
    if outline:
        cleaned_outline = builder._clean_title_text(outline)
        if cleaned_outline and not is_low_quality_title(cleaned_outline):
            return cleaned_outline

    tool = str(meta.get("tool") or "")
    fallback = _first_real_user_prompt(builder, messages, tool=tool)
    if fallback:
        return fallback
    return f"Session {id8}"


def _needs_messages_for_title(meta: Dict[str, Any], builder: IndexBuilder) -> bool:
    native = (meta.get("title") or "").strip()
    cleaned_native = builder._clean_title_text(native)
    if _library_native_title_usable(cleaned_native):
        return False
    outline = (meta.get("prompt_outline") or "").strip()
    if outline:
        cleaned_outline = builder._clean_title_text(outline)
        if cleaned_outline and not is_low_quality_title(cleaned_outline):
            return False
    return True


def _attach_display_titles(
    output_dir: Path,
    entries: Dict[str, _SessionEntry],
    old_sessions: Optional[Dict[str, Any]] = None,
) -> None:
    builder = IndexBuilder(output_dir)
    prev_map = old_sessions or {}
    draft_rows: List[Dict[str, Any]] = []
    pending_sids: List[str] = []
    for sid, entry in entries.items():
        prev = prev_map.get(sid) or {}
        stored_title = str(prev.get("display_title") or "")
        if prev.get("fingerprint") == entry.fingerprint and stored_title:
            entries[sid] = replace(entry, display_title=stored_title)
            continue
        messages: List[Any] = []
        if entry.has_body and _needs_messages_for_title(entry.meta, builder):
            loaded = load_session_messages_v2(output_dir, sid, include_reasoning=True)
            messages = loaded or []
        row = dict(entry.meta)
        row["id"] = sid
        row["title"] = _library_base_title(entry.meta, messages, builder)
        pending_sids.append(sid)
        draft_rows.append(row)
    for sid, row, annotated in zip(
        pending_sids,
        draft_rows,
        annotate_display_titles(draft_rows),
        strict=True,
    ):
        ent = entries.get(sid)
        if ent is None:
            continue
        entries[sid] = replace(
            ent,
            display_title=str(annotated.get("display_title") or row.get("title") or ""),
        )


def _entry_title(entry: _SessionEntry) -> str:
    return entry.display_title or str(entry.meta.get("title") or entry.meta.get("id") or "")


def _mark_published_entries(
    library_dir: Path,
    entries_map: Dict[str, _SessionEntry],
    old_sessions: Dict[str, Any],
    written_ids: Set[str],
) -> None:
    for sid, ent in entries_map.items():
        if sid in written_ids:
            entries_map[sid] = replace(ent, published=True)
            continue
        prev = old_sessions.get(sid) or {}
        rel = prev.get("path")
        if rel and (library_dir / f"{rel}.md").is_file():
            entries_map[sid] = replace(ent, published=True)


def _bounded_slug(value: str, *, fallback: str = "untitled") -> str:
    cleaned = sanitize_filename(value or fallback).strip("-")
    cleaned = re.sub(r"[\[\]|]", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) <= _MAX_SLUG:
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned[: _MAX_SLUG - 9]}-{digest}"


def _safe_component(name: str) -> str:
    name = (name or "").replace("\0", "")
    name = name.replace("..", "-")
    name = re.sub(r'[<>:"/\\|?*]', "-", name)
    name = name.strip(". ")
    if name in ("", ".", ".."):
        return "_"
    return name[:120]


def _resolve_under(root: Path, *parts: str) -> Path:
    safe = [_safe_component(p) for p in parts]
    target = root.joinpath(*safe).resolve()
    root_resolved = root.resolve()
    if not target.is_relative_to(root_resolved):
        raise ValueError(f"path escapes library root: {target}")
    return target


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _yaml_scalar(value: str) -> str:
    if not value:
        return '""'
    if any(ch in value for ch in ":\n#\"'[]{}"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _escape_wikilink_display(text: str) -> str:
    return (text or "Session").translate(_WIKILINK_ESCAPE)


def _parse_iso(value: Optional[str], fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return fallback


def _session_day(meta: Dict[str, Any]) -> str:
    created = meta.get("created") or meta.get("updated") or ""
    try:
        return datetime.fromisoformat(created).strftime("%Y-%m-%d")
    except ValueError:
        return "unknown-day"


def _one_liner(output_dir: Path, session_id: str) -> Optional[str]:
    row = get_summary(output_dir, "session", f"session:{session_id}")
    if not row:
        return None
    payload = row.get("payload") or {}
    one = payload.get("one_liner")
    return one if isinstance(one, str) and one.strip() else None


def _fingerprint(meta: Dict[str, Any], tags: List[str], summary: Optional[str]) -> str:
    blob = "\x1f".join(
        [
            str(meta.get("updated") or ""),
            str(meta.get("messages") or 0),
            str(meta.get("title") or ""),
            str(meta.get("prompt_outline") or ""),
            "|".join(sorted(tags)),
            summary or "",
        ]
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _session_stem(meta: Dict[str, Any], *, title: Optional[str] = None) -> Tuple[str, str]:
    day = _session_day(meta)
    title = title or meta.get("title") or meta.get("id") or "session"
    title_slug = _bounded_slug(str(title))
    sid = str(meta.get("id") or "")
    # The id's tail: time-ordered ids (UUIDv7, OpenCode ses_…) share their first characters.
    tail = re.sub(r"[^A-Za-z0-9]", "", sid)[-12:] or "unknown"
    stem = _safe_component(f"{day} {title_slug} {tail}")
    rel = f"{_SESSIONS}/{_project_slug(meta)}/{stem}"
    return stem, rel


def _project_slug(meta: Dict[str, Any]) -> str:
    raw = project_to_dirname(meta.get("project"))
    parts = [p for p in raw.split("-") if p and p not in {".", ".."}]
    return _bounded_slug(_safe_component("-".join(parts) or "_unknown"), fallback="_unknown")


def _build_unified(output_dir: Path, meta: Dict[str, Any]) -> Optional[UnifiedSession]:
    session_id = str(meta.get("id") or "")
    messages = load_session_messages_v2(output_dir, session_id, include_reasoning=True)
    if not messages:
        return None
    created_at = _parse_iso(meta.get("created"), messages[0].timestamp)
    summary = _one_liner(output_dir, session_id)
    return UnifiedSession(
        tool=_tool_from_value(meta.get("tool")),
        session_id=session_id,
        created_at=created_at,
        last_updated=_parse_iso(meta.get("updated"), created_at),
        messages=messages,
        project_path=meta.get("project"),
        thread_id=meta.get("thread_id"),
        title=meta.get("title"),
        summary=summary,
        git_branch=meta.get("git_branch"),
        git_commit=meta.get("git_commit"),
        origin=str(meta.get("origin") or "unknown"),
    )


def _inject_frontmatter(md: str, extra: Dict[str, str]) -> str:
    if not md.startswith("---\n"):
        return md
    end = md.find("\n---\n", 4)
    if end < 0:
        return md
    head = md[4:end]
    tail = md[end:]
    for key, value in extra.items():
        if value:
            head += f"\n{key}: {_yaml_scalar(value)}"
    return "---\n" + head + tail


def _append_see_also(md: str, project_slug: str, day: str) -> str:
    line = f"\n\nSee also: [[{_PROJECTS}/{project_slug}]] · [[{_DAYS}/{day}]]\n"
    return md.rstrip() + line


def _render_session_page(
    output_dir: Path,
    library_dir: Path,
    entry: _SessionEntry,
) -> str:
    session = _build_unified(output_dir, entry.meta)
    if session is None:
        return ""
    display_title = _entry_title(entry)
    session = replace(session, title=_escape_wikilink_display(display_title))
    exporter = MarkdownExporter(library_dir)
    md = exporter._generate_markdown(session)
    tags_line = ", ".join(entry.tags)
    extra = {
        "title": display_title,
        "tags": tags_line,
        "summary": entry.summary or "",
        "project_page": f"[[{_PROJECTS}/{entry.project_slug}]]",
        "day_page": f"[[{_DAYS}/{entry.day}]]",
    }
    md = _inject_frontmatter(md, extra)
    return _append_see_also(md, entry.project_slug, entry.day)


def _hidden_session_ids(output_dir: Path) -> Set[str]:
    """Tombstoned and pruned ids — same union as the default web session list."""
    deleted_path = output_dir / "deleted_sessions.json"
    return load_deleted_session_ids(deleted_path) | load_ignored_session_ids(output_dir)


def _meta_matches_library_scope(
    meta: Dict[str, Any],
    *,
    project: Optional[str],
    since: Optional[datetime],
) -> bool:
    if project is not None and meta.get("project") != project:
        return False
    if since is not None:
        updated = _parse_iso(meta.get("updated"), datetime.min)
        if make_naive(updated) < make_naive(since):
            return False
    return True


def _library_scope_active(project: Optional[str], since: Optional[datetime]) -> bool:
    return project is not None or since is not None


def _eligible_sessions(
    output_dir: Path,
    *,
    project: Optional[str],
    since: Optional[datetime],
) -> List[Dict[str, Any]]:
    if not v2_db_path(output_dir).exists():
        return []
    payload = load_index_v2(output_dir)
    hidden = _hidden_session_ids(output_dir)
    rows: List[Dict[str, Any]] = []
    for meta in payload.get("sessions", []):
        sid = str(meta.get("id") or "")
        if sid in hidden:
            continue
        if _library_skip_session(meta):
            continue
        if not _meta_matches_library_scope(meta, project=project, since=since):
            continue
        rows.append(meta)
    rows.sort(key=lambda m: str(m.get("updated") or ""), reverse=True)
    return rows


def _manifest_entry_for(ent: _SessionEntry) -> Dict[str, Any]:
    return {
        "fingerprint": ent.fingerprint,
        "path": f"{_SESSIONS}/{ent.project_slug}/{ent.stem}",
        "project_slug": ent.project_slug,
        "day": ent.day,
        "display_title": ent.display_title,
    }


def _merge_manifest_sessions(
    *,
    scope_active: bool,
    old_sessions: Dict[str, Any],
    entries_map: Dict[str, _SessionEntry],
    removed_ids: Set[str],
) -> Dict[str, Any]:
    if not scope_active:
        return {
            sid: _manifest_entry_for(ent)
            for sid, ent in entries_map.items()
            if ent.published and sid not in removed_ids
        }
    merged: Dict[str, Any] = {}
    for sid, prev in old_sessions.items():
        if sid in removed_ids:
            continue
        if sid not in entries_map:
            merged[sid] = prev
    for sid, ent in entries_map.items():
        if not ent.published or sid in removed_ids:
            continue
        merged[sid] = _manifest_entry_for(ent)
    return merged


def _index_entries(
    output_dir: Path,
    metas: Iterable[Dict[str, Any]],
    tags_map: Dict[str, List[str]],
    old_sessions: Optional[Dict[str, Any]] = None,
) -> Dict[str, _SessionEntry]:
    out: Dict[str, _SessionEntry] = {}
    for meta in metas:
        sid = str(meta.get("id") or "")
        tags = list(tags_map.get(sid, []))
        summary = _one_liner(output_dir, sid)
        fp = _fingerprint(meta, tags, summary)
        day = _session_day(meta)
        slug = _project_slug(meta)
        has_body = int(meta.get("messages") or 0) > 0
        out[sid] = _SessionEntry(
            meta=meta,
            tags=tags,
            summary=summary,
            fingerprint=fp,
            project_slug=slug,
            day=day,
            stem="",
            rel_link="",
            has_body=has_body,
        )
    _attach_display_titles(output_dir, out, old_sessions)
    for sid, entry in out.items():
        stem, rel = _session_stem(entry.meta, title=_entry_title(entry))
        out[sid] = replace(entry, stem=stem, rel_link=rel)
    return out


def _load_manifest(library_dir: Path) -> Dict[str, Any]:
    path = library_dir / _MANIFEST
    if not path.is_file():
        return {"sessions": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"sessions": {}}


def _save_manifest(library_dir: Path, manifest: Dict[str, Any]) -> None:
    _atomic_write(library_dir / _MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))


def _session_file_path(library_dir: Path, entry: _SessionEntry) -> Path:
    return _resolve_under(library_dir, _SESSIONS, entry.project_slug, f"{entry.stem}.md")


def _render_project_page(
    output_dir: Path,
    library_dir: Path,
    project_path: str,
    entries: List[_SessionEntry],
) -> str:
    slug = _bounded_slug(project_to_dirname(project_path), fallback="_unknown")
    label = _library_project_label(project_path or slug)
    full_path = project_path or slug
    lines = [
        "---",
        f"project: {_yaml_scalar(full_path)}",
        "---",
        "",
        f"# {label}",
        "",
        f"_{full_path}_",
        "",
        f"**{len(entries)}** sessions with library pages",
        "",
        "## Sessions",
        "",
    ]
    for entry in sorted(
        entries,
        key=lambda e: str(e.meta.get("updated") or ""),
        reverse=True,
    ):
        title = _escape_wikilink_display(_entry_title(entry))
        day = entry.day
        tool = str(entry.meta.get("tool") or "")
        summary = entry.summary or ""
        lines.append(f"- [[{entry.rel_link}|{title}]] · {day} · {tool} · {summary}".rstrip(" ·"))
    lines.append("")
    memories = select_context_memories(output_dir, scope_project=project_path, limit=40)
    if memories:
        lines.append("## Memory")
        lines.append("")
        for item in memories:
            lines.extend(_memory_lines(item))
        lines.append("")
    todos = [
        item
        for item in list_memory(output_dir, kind="todo", status="confirmed", limit=500)
        if item.scope_project == project_path
    ]
    if todos:
        lines.append("## Open todos")
        lines.append("")
        for item in todos:
            lines.extend(_memory_lines(item))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _memory_lines(item: MemoryEntry) -> List[str]:
    title = (item.title or item.kind or "memory").strip()
    body = (item.body or "").strip().splitlines()
    first = body[0] if body else ""
    return [f"- **{title}** — {first}".rstrip(" —")]


def _render_day_page(day: str, by_project: Dict[str, List[_SessionEntry]]) -> str:
    lines = [f"# {day}", ""]
    for project_path in sorted(by_project):
        entries = by_project[project_path]
        lines.append(f"## {project_path or '_unknown'}")
        lines.append("")
        for entry in sorted(
            entries,
            key=lambda e: str(e.meta.get("updated") or ""),
            reverse=True,
        ):
            title = _escape_wikilink_display(_entry_title(entry))
            tool = str(entry.meta.get("tool") or "")
            summary = entry.summary or ""
            lines.append(f"- [[{entry.rel_link}|{title}]] · {tool} · {summary}".rstrip(" ·"))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _render_index(
    output_dir: Path,
    entries: List[_SessionEntry],
) -> str:
    published = [e for e in entries if e.published]
    by_project: Dict[str, Dict[str, Any]] = {}
    by_day: Dict[str, int] = {}
    scratch_projects: Dict[str, Dict[str, Any]] = {}
    for entry in published:
        project = str(entry.meta.get("project") or "")
        slug = entry.project_slug
        bucket = scratch_projects if _is_scratch_shelf_project(project) else by_project
        slot = bucket.setdefault(
            slug,
            {"path": project, "count": 0, "last": "", "label": _library_project_label(project)},
        )
        slot["count"] += 1
        updated = str(entry.meta.get("updated") or "")
        if updated > slot["last"]:
            slot["last"] = updated
        by_day[entry.day] = by_day.get(entry.day, 0) + 1
    lines = ["# Lore library", ""]
    lines.append("## Projects")
    lines.append("")
    for slug, info in sorted(by_project.items(), key=lambda kv: kv[1]["last"], reverse=True):
        lines.append(
            f"- [[{_PROJECTS}/{slug}|{info['label']}]] — "
            f"{info['count']} sessions · last {info['last'][:10]}"
        )
    if scratch_projects:
        lines.append("")
        lines.append(f"## {_SCRATCH_SHELF}")
        lines.append("")
        for slug, info in sorted(
            scratch_projects.items(), key=lambda kv: kv[1]["last"], reverse=True
        ):
            lines.append(
                f"- [[{_PROJECTS}/{slug}|{info['label']}]] — "
                f"{info['count']} sessions · last {info['last'][:10]}"
            )
    lines.append("")
    lines.append("## Recent days")
    lines.append("")
    for day in sorted(by_day, reverse=True)[:14]:
        lines.append(f"- [[{_DAYS}/{day}|{day}]] — {by_day[day]} sessions")
    tag_counts = list_all_tags(output_dir)
    if tag_counts:
        lines.append("")
        lines.append("## Top tags")
        lines.append("")
        for tag, count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:15]:
            lines.append(f"- `{tag}` — {count}")
    lines.append("")
    return "\n".join(lines)


def build_library(
    output_dir: Path,
    library_dir: Path,
    *,
    full: bool = False,
    project: Optional[str] = None,
    since: Optional[datetime] = None,
) -> LibraryStats:
    """Write or incrementally update the on-disk library under ``library_dir``."""
    library_dir = library_dir.expanduser().resolve()
    library_dir.mkdir(parents=True, exist_ok=True)
    stats = LibraryStats()

    scope_active = _library_scope_active(project, since)
    all_metas = _eligible_sessions(output_dir, project=None, since=None)
    scoped_metas = _eligible_sessions(output_dir, project=project, since=since)
    all_eligible_ids = {str(m.get("id") or "") for m in all_metas}
    scoped_eligible_ids = {str(m.get("id") or "") for m in scoped_metas}
    store_by_id = {str(m.get("id") or ""): m for m in load_index_v2(output_dir).get("sessions", [])}

    manifest = _load_manifest(library_dir)
    old_sessions: Dict[str, Any] = manifest.get("sessions") or {}

    tags_map = all_tags_by_session(output_dir)
    entries_map = _index_entries(output_dir, scoped_metas, tags_map, old_sessions)

    old_ids = set(old_sessions)
    if scope_active:
        removed_ids = {
            sid
            for sid in old_ids
            if (meta := store_by_id.get(sid)) is not None
            and _meta_matches_library_scope(meta, project=project, since=since)
            and sid not in all_eligible_ids
        }
    else:
        removed_ids = old_ids - scoped_eligible_ids

    to_write: Set[str] = set()
    if full:
        to_write = {sid for sid, ent in entries_map.items() if ent.has_body}
    else:
        for sid, ent in entries_map.items():
            if not ent.has_body:
                continue
            prev = old_sessions.get(sid) or {}
            if prev.get("fingerprint") != ent.fingerprint:
                to_write.add(sid)
            else:
                stats.skipped += 1

    affected_projects: Set[str] = set()
    affected_days: Set[str] = set()

    for sid in removed_ids:
        prev = old_sessions.get(sid) or {}
        affected_projects.add(str(prev.get("project_slug") or ""))
        affected_days.add(str(prev.get("day") or ""))
        rel = prev.get("path")
        if rel:
            target = library_dir / f"{rel}.md"
            if target.is_file():
                target.unlink()

    written_ids: Set[str] = set()
    for sid in to_write:
        ent = entries_map[sid]
        affected_projects.add(ent.project_slug)
        affected_days.add(ent.day)
        content = _render_session_page(output_dir, library_dir, ent)
        if not content:
            continue
        prev = old_sessions.get(sid) or {}
        new_rel = f"{_SESSIONS}/{ent.project_slug}/{ent.stem}"
        old_rel = str(prev.get("path") or "")
        if old_rel and old_rel != new_rel:
            stale = (library_dir / f"{old_rel}.md").resolve()
            root = library_dir.resolve()
            try:
                if stale.is_relative_to(root) and stale.is_file():
                    stale.unlink()
            except ValueError:
                pass
        path = _session_file_path(library_dir, ent)
        _atomic_write(path, content)
        stats.sessions_written += 1
        written_ids.add(sid)

    _mark_published_entries(library_dir, entries_map, old_sessions, written_ids)

    full_entries_map = _index_entries(output_dir, all_metas, tags_map, old_sessions)
    manifest_sessions = _merge_manifest_sessions(
        scope_active=scope_active,
        old_sessions=old_sessions,
        entries_map=entries_map,
        removed_ids=removed_ids,
    )
    published_ids = set(manifest_sessions)

    carried_ids = published_ids - set(entries_map)
    for sid in carried_ids:
        prev = old_sessions.get(sid) or {}
        rel = prev.get("path")
        if rel and (library_dir / f"{rel}.md").is_file():
            ent = full_entries_map.get(sid)
            if ent is not None:
                full_entries_map[sid] = replace(ent, published=True)

    for sid in published_ids:
        ent = entries_map.get(sid)
        if ent is not None:
            full_entries_map[sid] = ent

    entries = [full_entries_map[sid] for sid in published_ids if sid in full_entries_map]

    if full:
        affected_projects = {e.project_slug for e in entries}
        affected_days = {e.day for e in entries}
    else:
        for sid in to_write | removed_ids:
            ent = full_entries_map.get(sid)
            if ent:
                affected_projects.add(ent.project_slug)
                affected_days.add(ent.day)

    projects_index: Dict[str, List[_SessionEntry]] = {}
    for ent in entries:
        if not ent.published:
            continue
        project_path = str(ent.meta.get("project") or "")
        projects_index.setdefault(project_path, []).append(ent)

    for slug in sorted(p for p in affected_projects if p):
        project_path = next(
            (str(e.meta.get("project") or "") for e in entries if e.project_slug == slug),
            slug,
        )
        published_for_project = projects_index.get(project_path, [])
        page = _render_project_page(
            output_dir,
            library_dir,
            project_path,
            published_for_project,
        )
        _atomic_write(_resolve_under(library_dir, _PROJECTS, f"{slug}.md"), page)
        stats.projects_written += 1

    days_index: Dict[str, Dict[str, List[_SessionEntry]]] = {}
    for ent in entries:
        if not ent.published:
            continue
        days_index.setdefault(ent.day, {}).setdefault(
            str(ent.meta.get("project") or ""), []
        ).append(ent)

    for day in sorted(d for d in affected_days if d):
        page = _render_day_page(day, days_index.get(day, {}))
        _atomic_write(_resolve_under(library_dir, _DAYS, f"{day}.md"), page)
        stats.days_written += 1

    _atomic_write(library_dir / "Index.md", _render_index(output_dir, entries))

    _save_manifest(library_dir, {"sessions": manifest_sessions})
    return stats
