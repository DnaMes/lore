import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..core.models import UnifiedSession

logger = logging.getLogger(__name__)


def _stat_mtime_ns(path_value: Optional[str]) -> Optional[int]:
    """Return the mtime_ns of ``path_value`` if it exists, else None."""
    if not path_value:
        return None
    try:
        return os.stat(path_value).st_mtime_ns
    except OSError:
        return None


def is_low_quality_title(text: str) -> bool:
    """True if ``text`` is noise that should not be used as a session title.

    Catches command/caveat boilerplate, sandbox/approval chatter, and too-short or
    non-alphabetic strings. Used both when building the index and when rendering a
    live session, so the two paths agree (#68).
    """
    lowered = (text or "").lower().strip()
    if not lowered:
        return True

    # Drop any leading XML-ish tag wrapper (e.g. "<local-command-caveat>") so the
    # boilerplate it wraps is still recognised below (#68).
    lowered = re.sub(r"^<[^>]*>", "", lowered).strip()
    if not lowered:
        return True

    normalized = re.sub(r"^[^a-z0-9]+", "", lowered)

    low_quality_prefixes = (
        "local-command-",
        "caveat: the messages below were generated",
        "user exited claude code session",
        "conversation started",
        "session ",
        "agents.md instructions",
        "instructions for /home",
        "command-name",
        "command-message",
        "command-args",
        "login successful",
        "invalid api key",
        "api error",
        "generate a file named agents.md",
    )
    if any(normalized.startswith(prefix) for prefix in low_quality_prefixes):
        return True

    low_quality_fragments = (
        "on-request workspace-write",
        "workspace-write restricted",
        "sandbox",
        "approval",
    )
    if any(fragment in normalized for fragment in low_quality_fragments):
        return True

    if len(lowered) < 8:
        return True

    alpha_chars = sum(1 for ch in lowered if ch.isalpha())
    if alpha_chars < max(3, len(lowered) // 6):
        return True

    return False


class IndexBuilder:
    """Build search index for sessions."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.index_path = output_dir / "index.json"
        self.sqlite_path = output_dir / "index.sqlite"

    def build_index(
        self,
        sessions: Iterable[UnifiedSession],
        export_paths: Dict[str, Path],
        reused_entries: Optional[List[Dict]] = None,
        reused_sessions: Optional[Iterable[UnifiedSession]] = None,
        reused_ids: Optional[set] = None,
    ) -> None:
        """Build and save the search index, streaming ``sessions`` once (#96).

        ``sessions`` may be a generator: it is consumed in a single pass that
        fans each session out to the JSON index, the legacy ``index.sqlite``,
        and the v2 store, then drops it — so a full rebuild no longer holds
        every ``UnifiedSession`` (with message bodies) in RAM at once.

        When ``reused_entries`` is provided, those pre-built session dicts are
        included verbatim in the JSON index (no re-extraction of
        keywords/search_text). They must be disjoint from ``sessions`` by id.

        Two ways to supply the *full* UnifiedSession objects behind the reused
        entries so the v2 store gets their message rows (#35):

        - ``reused_ids`` (preferred, #103): a set of ids that arrive **inside**
          ``sessions``. Sessions whose id is in this set are written v2-only
          (their JSON/legacy entry comes from ``reused_entries``), so the caller
          streams one merged generator and never holds a second list in RAM.
        - ``reused_sessions``: a separate iterable of full sessions, streamed
          after ``sessions``. Kept for the list-based callers/tests.
        """
        ignored_ids = self._load_ignored()
        if ignored_ids and reused_entries:
            reused_entries = [
                entry for entry in reused_entries if entry.get("id") not in ignored_ids
            ]
        # NB: do NOT `reused_ids or set()` — the caller may pass a live set that
        # is still empty at call time and fills as the `sessions` generator runs
        # (services.extraction). `or set()` would swap in a new empty set and
        # drop the reference, mis-routing every reused session to a full write.
        if reused_ids is None:
            reused_ids = set()

        writer = _MultiWriter(self, reused_entries=reused_entries)
        try:
            for session in sessions:
                if session.session_id in ignored_ids:
                    continue
                if session.session_id in reused_ids:
                    # Reused: v2-only (JSON/legacy entry comes from
                    # reused_entries). Written and dropped one at a time so warm
                    # incremental sync never holds every unchanged session in RAM
                    # to re-write its v2 message rows (#96/#103/#35).
                    writer.add_reused_session(session, export_paths.get(session.session_id, ""))
                else:
                    # Fresh/refreshed: full fan-out (JSON + legacy + v2).
                    writer.add_session(session, export_paths.get(session.session_id, ""))
            # Legacy list-based path: reused full sessions supplied separately.
            for session in reused_sessions or []:
                if session.session_id in ignored_ids:
                    continue
                writer.add_reused_session(session, export_paths.get(session.session_id, ""))
            writer.finalize()
        finally:
            writer.close()

    def _compute_stats_from_entries(self, entries: List[Dict]) -> Dict:
        """Compute statistics from already-built session dict entries."""
        by_tool: Dict[str, int] = {}
        by_project: Dict[str, int] = {}
        total_messages = 0
        for entry in entries:
            tool_name = str(entry.get("tool") or "")
            if tool_name:
                by_tool[tool_name] = by_tool.get(tool_name, 0) + 1
            project = entry.get("project")
            if project:
                by_project[project] = by_project.get(project, 0) + 1
            total_messages += int(entry.get("messages") or 0)
        return {
            "total_sessions": len(entries),
            "total_messages": total_messages,
            "by_tool": by_tool,
            "by_project": by_project,
        }

    def _compute_stats(self, sessions: List[UnifiedSession]) -> Dict:
        """Compute statistics from sessions."""
        by_tool: Dict[str, int] = {}
        by_project: Dict[str, int] = {}

        for session in sessions:
            tool_name = session.tool.value
            by_tool[tool_name] = by_tool.get(tool_name, 0) + 1

            if session.project_path:
                by_project[session.project_path] = by_project.get(session.project_path, 0) + 1

        return {
            "total_sessions": len(sessions),
            "total_messages": sum(s.message_count for s in sessions),
            "by_tool": by_tool,
            "by_project": by_project,
        }

    def _count_prompts(self, session: UnifiedSession) -> int:
        return session.user_prompt_count

    def _extract_prompt_outline(self, session: UnifiedSession) -> str:
        skip_markers = (
            "caveat: the messages below were generated by the user",
            "do not respond to these messages",
            "messages below were generated by the user while running local commands",
            "<command-name>",
            "<command-message>",
            "<command-args>",
            "# agents.md instructions",
            "agents.md instructions",
            "## instructions",
        )
        for msg in session.messages:
            role = getattr(msg, "role", None)
            if getattr(role, "value", role) != "user":
                continue
            text = (msg.content or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if any(marker in lowered for marker in skip_markers):
                continue
            text = self._clean_title_text(text)
            if not text:
                continue
            if self._is_low_quality_title(text):
                continue
            return text[:140]
        return ""

    def _infer_title(self, session: UnifiedSession, prompt_outline: str) -> str:
        suffix = session.session_id[-8:] if session.session_id else "session"

        if session.title and session.title.strip():
            native = self._clean_title_text(session.title)
            if native and not self._is_low_quality_title(native):
                return native[:80]

        if prompt_outline:
            cleaned = self._clean_title_text(prompt_outline)
            if self._is_low_quality_title(cleaned):
                cleaned = ""
            if cleaned:
                base = cleaned[:64]
                if base.endswith(f"· {suffix}"):
                    return base
                return f"{base} · {suffix}"

        date_label = session.created_at.strftime("%Y-%m-%d")
        tool_label = session.tool.value.replace("-", " ").title()
        return f"{tool_label} {date_label} • {suffix}"

    def _clean_title_text(self, text: str) -> str:
        cleaned = re.sub(r"^\[Tool Result\]\s*", "", text or "").strip()
        cleaned = re.sub(r"```.*?```", " ", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = cleaned.replace("`", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def _is_low_quality_title(self, text: str) -> bool:
        return is_low_quality_title(text)

    def _extract_keywords(self, session: UnifiedSession) -> List[str]:
        """Extract searchable keywords from session."""
        keywords = set()

        # From title
        if session.title:
            words = re.findall(r"\b\w{3,}\b", session.title.lower())
            keywords.update(words)

        # From messages (limited to avoid huge indexes)
        for msg in session.messages[:20]:  # First 20 messages
            if not msg.content:
                continue
            words = re.findall(r"\b\w{4,}\b", msg.content.lower())
            keywords.update(words[:50])  # First 50 words per message

        # Filter out common words
        stopwords = {
            "this",
            "that",
            "with",
            "have",
            "will",
            "from",
            "they",
            "been",
            "were",
            "said",
            "each",
            "which",
            "their",
            "there",
            "would",
            "about",
        }
        keywords -= stopwords

        return list(keywords)[:100]  # Limit to 100 keywords

    def _build_search_text(self, session: UnifiedSession) -> str:
        parts: List[str] = []

        if session.title:
            parts.append(session.title)

        for msg in session.messages[:30]:
            if msg.content:
                parts.append(msg.content)

        text = " ".join(parts).lower()
        return text[:20000]

    def _open_legacy_sqlite(self) -> sqlite3.Connection:
        """Open + reset the legacy ``index.sqlite`` and return the connection.

        Creates the schema/indexes if absent and clears the ``sessions`` +
        ``sessions_fts`` tables so the caller can stream fresh rows in. The
        connection is returned uncommitted; the caller owns commit/close.
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.sqlite_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                tool TEXT,
                project TEXT,
                thread_id TEXT,
                title TEXT,
                created TEXT,
                updated TEXT,
                messages INTEGER,
                prompts INTEGER,
                prompt_outline TEXT,
                export_path TEXT,
                search_text TEXT
            )
        """
        )
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "prompts" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN prompts INTEGER")
        if "prompt_outline" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN prompt_outline TEXT")
        conn.execute("DELETE FROM sessions")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_tool ON sessions(tool)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_thread_id ON sessions(thread_id)")

        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts
            USING fts5(id, title, project, tool, search_text)
        """
        )
        conn.execute("DELETE FROM sessions_fts")
        return conn

    def _load_ignored(self) -> set:
        ignore_path = self.output_dir / "ignored.json"
        if not ignore_path.exists():
            return set()
        try:
            data = json.loads(ignore_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                values = data.get("session_ids", [])
            else:
                values = data
            return set(values or [])
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            # A corrupt/unreadable ignore list must not silently un-prune
            # sessions (#116) — warn so the operator can fix the file.
            logger.warning(
                "Could not load ignored.json (%s); previously pruned "
                "sessions may reappear in the next rebuild",
                exc,
            )
            return set()


_LEGACY_SESSION_INSERT = """
    INSERT OR REPLACE INTO sessions (
        id, tool, project, thread_id, title, created, updated,
        messages, prompts, prompt_outline, export_path, search_text
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_LEGACY_FTS_INSERT = """
    INSERT INTO sessions_fts (id, title, project, tool, search_text)
    VALUES (?, ?, ?, ?, ?)
"""


class _MultiWriter:
    """Fan one streamed session out to all index consumers (issue #96).

    Owns the JSON accumulator + keyword map, the legacy ``index.sqlite``
    connection, and a :class:`StreamingV2Writer` for the v2 store. Each
    :meth:`add_session` computes the per-session derived values (title, outline,
    keywords, search_text) **once** and feeds all three — the old three-pass
    ``build_index`` recomputed several of them twice.

    Memory: only *light* per-session data (JSON dicts + legacy row tuples, no
    message bodies) accumulates; it is written to disk in one shot at finalize.
    The *heavy* data (message rows) streams straight into v2 per session and is
    dropped with the session — this is what bounds peak RSS regardless of
    archive size.

    Order matches the pre-#96 list path exactly: reused-entry dicts are placed
    first (JSON + legacy + v2-metadata), then streamed sessions in arrival
    order, then any ``reused_sessions`` (v2-only full rows). Because the
    streaming caller discovers reused entries *while* the refresh generator
    runs, reused rows are seeded at :meth:`finalize` and prepended — preserving
    the ordered ``sessions[0]`` / id-list assertions the tests rely on.
    """

    def __init__(
        self,
        builder: "IndexBuilder",
        *,
        reused_entries: Optional[List[Dict]],
    ) -> None:
        from ..storage.writer import StreamingV2Writer, v2_db_path

        self.builder = builder
        # reused_entries may be a live list the caller fills *while* the refresh
        # generator streams (see services.extraction). It is seeded in
        # finalize() — after the refresh drain — so we only keep the reference
        # here; reading it now would see an empty list.
        self._reused_entries = reused_entries
        self._full_reused_ids: set[str] = set()
        self.index: Dict = {
            "version": "1.0.0",
            "generated_at": datetime.now().isoformat(),
            "stats": {},
            "sessions": [],
            "search_index": {},
        }
        self.keyword_index: Dict[str, List[str]] = {}
        self._titles: Dict[str, str] = {}
        self._extras: Dict[str, Dict] = {}
        self._legacy_rows: List[tuple] = []
        self._legacy_fts_rows: List[tuple] = []
        self.legacy_conn = builder._open_legacy_sqlite()
        # titles/extras are filled as sessions stream; the v2 writer reads them
        # lazily per add, so passing the live dicts is fine.
        self.v2 = StreamingV2Writer(v2_db_path(builder.output_dir), self._titles, self._extras)
        self._v2_ok = True
        self._v2_done = False  # True once v2 has been finalized (commit + close)
        try:
            self.v2.begin()
        except Exception as exc:  # noqa: BLE001 - v2 is best-effort (#44)
            logger.warning("v2 dual-write skipped (begin failed, non-fatal): %s", exc)
            self._v2_ok = False

        # Pre-seed titles/extras from reused_entries that are ALREADY known at
        # construction (the list path). The streaming caller fills reused_entries
        # later; add_reused_session falls back to session.title for those, and
        # the v2 title override there stays consistent because the JSON title
        # for a reused entry comes from the same prior dict. Titles are light —
        # seeding them early doesn't retain sessions.
        for entry in reused_entries or []:
            sid = entry.get("id")
            if sid:
                self._titles[sid] = entry.get("title") or ""
                self._extras[sid] = {
                    "prompt_outline": entry.get("prompt_outline"),
                    "export_path": entry.get("export_path"),
                }

    def _seed_reused_entries(self) -> None:
        """Seed reused-entry rows (JSON + legacy + optional v2 metadata).

        Called at finalize so it works whether the caller passed reused_entries
        up front (list path) or filled the list during the refresh stream
        (services.extraction). Reused entries go to the FRONT of the JSON
        ``sessions`` array + legacy rows, preserving the pre-#96 order
        (reused-first, then refreshed).

        v2 metadata rows are written only for ids without a full reused session
        in this build. Complete existing reused ids use a metadata-preserving
        v2 update; incomplete/new ids receive their full message rows (#35).
        """
        entries = list(self._reused_entries or [])
        if not entries:
            return
        json_head = []
        legacy_head = []
        fts_head = []
        for entry in entries:
            json_head.append(entry)
            for kw in entry.get("keywords") or []:
                self.keyword_index.setdefault(kw, []).append(entry.get("id", ""))
            legacy_head.append(
                (
                    entry.get("id"),
                    entry.get("tool"),
                    entry.get("project"),
                    entry.get("thread_id"),
                    entry.get("title"),
                    entry.get("created"),
                    entry.get("updated"),
                    int(entry.get("messages") or 0),
                    int(entry.get("prompts") or 0),
                    entry.get("prompt_outline"),
                    entry.get("export_path"),
                    entry.get("search_text") or "",
                )
            )
            fts_head.append(
                (
                    entry.get("id"),
                    entry.get("title") or "",
                    entry.get("project") or "",
                    entry.get("tool") or "",
                    entry.get("search_text") or "",
                )
            )
            if entry.get("id") not in self._full_reused_ids:
                self._v2_add(lambda e=entry: self.v2.add_reused_entry(e))
        # Prepend so reused entries precede the streamed sessions in both the
        # JSON array and the buffered legacy rows (written in finalize).
        self.index["sessions"] = json_head + self.index["sessions"]
        self._legacy_rows = legacy_head + self._legacy_rows
        self._legacy_fts_rows = fts_head + self._legacy_fts_rows

    def _v2_add(self, op) -> None:
        """Run a v2 write op, disabling v2 for the run on first failure (#44)."""
        if not self._v2_ok:
            return
        try:
            op()
        except Exception as exc:  # noqa: BLE001 - v2 best-effort, never fatal
            logger.warning("v2 dual-write failed mid-stream (non-fatal): %s", exc)
            self.v2.abort()
            self._v2_ok = False

    def add_session(self, session: UnifiedSession, export_path) -> None:
        b = self.builder
        prompt_count = b._count_prompts(session)
        prompt_outline = b._extract_prompt_outline(session)
        inferred_title = b._infer_title(session, prompt_outline)
        keywords = b._extract_keywords(session)
        search_text = b._build_search_text(session)
        export_str = str(export_path) if export_path else None

        self._titles[session.session_id] = inferred_title
        self._extras[session.session_id] = {
            "prompt_outline": prompt_outline,
            "export_path": export_str,
        }

        self.index["sessions"].append(
            {
                "id": session.session_id,
                "tool": session.tool.value,
                "project": session.project_path,
                "thread_id": session.thread_id,
                "title": inferred_title,
                "created": session.created_at.isoformat(),
                "updated": session.last_updated.isoformat(),
                "messages": session.message_count,
                "prompts": prompt_count,
                "tokens": session.total_tokens,
                "prompt_outline": prompt_outline,
                "export_path": export_str,
                "git_branch": getattr(session, "git_branch", None),
                "git_commit": getattr(session, "git_commit", None),
                "source_path": session.source_path,
                "source_mtime": _stat_mtime_ns(session.source_path),
                "keywords": keywords,
                "search_text": search_text,
            }
        )
        for kw in keywords:
            self.keyword_index.setdefault(kw, []).append(session.session_id)

        self._legacy_rows.append(
            (
                session.session_id,
                session.tool.value,
                session.project_path,
                session.thread_id,
                inferred_title,
                session.created_at.isoformat(),
                session.last_updated.isoformat(),
                session.message_count,
                prompt_count,
                prompt_outline,
                export_str,
                search_text,
            )
        )
        self._legacy_fts_rows.append(
            (
                session.session_id,
                inferred_title or "",
                session.project_path or "",
                session.tool.value,
                search_text,
            )
        )
        # Legacy rows are light tuples (no message bodies) — buffer them and
        # write once at finalize so the reused-first ordering holds. The heavy
        # data (message rows) streams straight into v2 below and is dropped with
        # the session, which is what actually bounds memory (#96).
        self._v2_add(lambda: self.v2.add_full(session))

    def add_reused_session(self, session: UnifiedSession, export_path) -> None:
        """Write a reused session's full rows to the v2 store only (#35).

        Its JSON/legacy entry already came from a reused_entries dict; here we
        supply the v2 row and messages. A complete existing v2 row takes a
        metadata-only update so unchanged message rows are not rewritten.
        """
        if session.session_id not in self._titles:
            # Fall back to the session's own title if no reused entry seeded one.
            self._titles[session.session_id] = session.title or ""
        if session.session_id not in self._extras:
            export_str = str(export_path) if export_path else None
            self._extras[session.session_id] = {
                "prompt_outline": None,
                "export_path": export_str,
            }
        self._full_reused_ids.add(session.session_id)
        self._v2_add(lambda: self.v2.add_reused_full(session))

    def _flush_legacy(self) -> None:
        if self._legacy_rows:
            self.legacy_conn.executemany(_LEGACY_SESSION_INSERT, self._legacy_rows)
            self._legacy_rows.clear()
        if self._legacy_fts_rows:
            self.legacy_conn.executemany(_LEGACY_FTS_INSERT, self._legacy_fts_rows)
            self._legacy_fts_rows.clear()

    def finalize(self) -> None:
        # Seed reused entries now (after any refresh stream + reused_session
        # writes): prepends them to the JSON array + legacy rows so the output
        # order stays reused-first, and decides v2 metadata-vs-full per #35.
        self._seed_reused_entries()

        # Legacy JSON index — stats from the accumulated light dicts, then atomic
        # write (tmp + os.replace) so a SIGINT never leaves a partial index.json.
        from ..utils.paths import restrict_file, secure_dir

        self.index["stats"] = self.builder._compute_stats_from_entries(self.index["sessions"])
        self.index["search_index"] = self.keyword_index

        secure_dir(self.builder.output_dir)  # 0700 — holds session transcripts (#41)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.builder.output_dir,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            json.dump(self.index, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, self.builder.index_path)
        restrict_file(self.builder.index_path)  # 0600 — index.json holds content

        # Legacy sqlite — write all buffered rows (reused-first) + commit.
        self._flush_legacy()
        self.legacy_conn.commit()

        # v2 store — commit + post-commit embed (best-effort).
        if self._v2_ok:
            self._v2_add(self.v2.finalize)
            self._v2_done = True

    def close(self) -> None:
        try:
            self.legacy_conn.close()
        except sqlite3.Error:
            pass
        # If finalize() didn't run (exception path) and v2 is still open, roll it
        # back so a crashed build never leaves a half-written v2 store. A v2 that
        # already committed (or was disabled mid-stream) is left alone.
        if self._v2_ok and not self._v2_done:
            try:
                self.v2.abort()
            except Exception:  # noqa: BLE001 - close path, never fatal
                pass
