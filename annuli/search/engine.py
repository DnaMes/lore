import json
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from lore.core.models import AUTOMATED_ORIGINS
from lore.utils.security import validate_search_param, validate_tool_name
from lore.utils.tooling import normalize_tool_name

# Rows the SQLite path returns at most (the historical ``LIMIT 50``).
SQLITE_RESULT_LIMIT = 50
# The origin filter never widens the fetch past this many scanned rows (50, 200, 800, 2000):
# a table of mostly automated rows returns what was collected and sets ``last_search_truncated``.
SQLITE_MAX_SCANNED_ROWS = 2000

logger = logging.getLogger(__name__)


class SearchEngine:
    """Simple in-memory search engine."""

    def __init__(self, index_path: Path):
        self.index_path = index_path
        self.sqlite_path = index_path.with_suffix(".sqlite")
        self.index = self._load_index()
        # True when the last ``search`` hit the scan cap with fewer results than asked for.
        self.last_search_truncated = False

    def _load_index(self) -> Dict:
        """Load index from file."""
        if not self.index_path.exists():
            return {"sessions": [], "search_index": {}}

        with open(self.index_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _automated_ids(self) -> set:
        """Ids of automated sessions. The legacy SQLite table has no origin column,
        so the origin comes from the JSON index entries."""
        return {
            str(s.get("id"))
            for s in self.index.get("sessions", [])
            if s.get("origin") in AUTOMATED_ORIGINS
        }

    def search(
        self,
        query: str,
        tool: Optional[str] = None,
        project: Optional[str] = None,
        include_automated: bool = True,
    ) -> List[Dict]:
        """Search sessions by query.

        With ``include_automated=False`` exec/cron/subagent sessions are dropped before
        the result cap, so they cannot crowd interactive hits out of the first rows.
        """
        self.last_search_truncated = False
        tool = normalize_tool_name(tool) if tool else None
        excluded = set() if include_automated else self._automated_ids()
        if self.sqlite_path.exists():
            results = self._search_sqlite(query, tool=tool, project=project, excluded=excluded)
            if results:
                return results
        # Quotes only mark a phrase for the v2 store; this legacy path matches words.
        query_words = query.lower().replace('"', " ").split()
        results = []

        for session in self.index.get("sessions", []):
            if str(session.get("id")) in excluded:
                continue
            # Filter by tool
            if tool and session.get("tool") != tool:
                continue

            # Filter by project
            if project and session.get("project") != project:
                continue

            # Score by keyword matches
            score = 0
            session_keywords = set(session.get("keywords", []))
            search_text = session.get("search_text", "")

            for word in query_words:
                if word in session_keywords:
                    score += 5
                if session.get("title") and word in session.get("title", "").lower():
                    score += 10
                if search_text and word in search_text:
                    score += 2

            if score > 0:
                results.append(
                    {
                        "session": session,
                        "score": score,
                    }
                )

        # Sort by score descending
        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    def _search_sqlite(
        self,
        query: str,
        tool: Optional[str] = None,
        project: Optional[str] = None,
        excluded: Optional[set] = None,
    ) -> List[Dict]:
        tool = normalize_tool_name(tool) if tool else None
        # A quoted phrase reaches this fallback unchanged: drop the quotes so every
        # term is a plain quoted FTS token (adjacency is not enforced on this path).
        terms = [t for t in query.lower().replace('"', " ").split() if t]
        if not terms:
            return []
        fts_query = " ".join(f'"{t}"' for t in terms)

        where_clauses = []
        params = [fts_query]

        # Validate tool parameter to prevent SQL injection
        if tool:
            if not validate_tool_name(tool):
                # Invalid tool name - reject query
                return []
            where_clauses.append("s.tool = ?")
            params.append(tool)

        # Validate project parameter to prevent SQL injection
        if project:
            if not validate_search_param(project):
                # Invalid project parameter - reject query
                return []
            where_clauses.append("s.project = ?")
            params.append(project)
        where_sql = ""
        if where_clauses:
            where_sql = " AND " + " AND ".join(where_clauses)

        sql = f"""
            SELECT s.id, s.tool, s.project, s.thread_id, s.title, s.created,
                   s.updated, s.messages, s.export_path, bm25(sessions_fts) AS score
            FROM sessions_fts
            JOIN sessions s ON s.id = sessions_fts.id
            WHERE sessions_fts MATCH ? {where_sql}
            ORDER BY score ASC
            LIMIT ?
        """

        # Over-fetch until ``SQLITE_RESULT_LIMIT`` rows survive the origin filter, the
        # table is exhausted (fewer rows than asked for come back) or a cap is reached.
        fetch = SQLITE_RESULT_LIMIT
        kept: List[tuple] = []
        conn = sqlite3.connect(self.sqlite_path)
        try:
            while True:
                try:
                    rows = conn.execute(sql, [*params, fetch]).fetchall()
                except sqlite3.Error:
                    return []
                kept = [row for row in rows if str(row[0]) not in (excluded or ())]
                if len(kept) >= SQLITE_RESULT_LIMIT or len(rows) < fetch:
                    break
                if fetch >= SQLITE_MAX_SCANNED_ROWS:
                    self.last_search_truncated = True
                    logger.warning(
                        "Search stopped at the %d-row scan cap with %d of %d results",
                        SQLITE_MAX_SCANNED_ROWS,
                        len(kept),
                        SQLITE_RESULT_LIMIT,
                    )
                    break
                fetch = min(fetch * 4, SQLITE_MAX_SCANNED_ROWS)
        finally:
            conn.close()

        results = []
        for row in kept[:SQLITE_RESULT_LIMIT]:
            session = {
                "id": row[0],
                "tool": row[1],
                "project": row[2],
                "thread_id": row[3],
                "title": row[4],
                "created": row[5],
                "updated": row[6],
                "messages": row[7],
                "export_path": row[8],
            }
            results.append({"session": session, "score": float(row[9])})
        return results
