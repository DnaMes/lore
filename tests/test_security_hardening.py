"""Security-hardening tests (#41, #45).

#41 — data files holding session transcripts must be owner-only.
#45 — state-changing POST routes must carry the cross-origin guard.
"""

from __future__ import annotations

import stat
from datetime import datetime

from annuli.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from annuli.exporters.index import IndexBuilder
from annuli.interfaces import web
from annuli.storage import v2_db_path
from annuli.utils.paths import restrict_file, secure_dir


def _session(sid="s1"):
    return UnifiedSession(
        tool=Tool.CLAUDE_CODE,
        session_id=sid,
        created_at=datetime(2026, 1, 1),
        last_updated=datetime(2026, 1, 2),
        project_path="/p",
        title=f"Session {sid} realistic title",
        messages=[UnifiedMessage(role=Role.USER, content="body", timestamp=datetime(2026, 1, 1))],
    )


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------
# #41 — file permissions
# ---------------------------------------------------------------------------


def test_secure_dir_is_owner_only(tmp_path):
    d = secure_dir(tmp_path / "store")
    # no group/other bits
    assert _mode(d) & 0o077 == 0


def test_restrict_file_removes_group_other(tmp_path):
    f = tmp_path / "data.json"
    f.write_text("{}", encoding="utf-8")
    f.chmod(0o644)
    restrict_file(f)
    assert _mode(f) & 0o077 == 0


def test_index_json_is_owner_only(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    index_json = tmp_path / "index.json"
    assert index_json.exists()
    assert _mode(index_json) & 0o077 == 0


def test_v2_db_is_owner_only(tmp_path):
    IndexBuilder(tmp_path).build_index([_session("a")], {})
    v2 = v2_db_path(tmp_path)
    assert v2.exists()
    assert _mode(v2) & 0o077 == 0


# ---------------------------------------------------------------------------
# #45 — cross-origin guard on POST routes
# ---------------------------------------------------------------------------


def test_noise_rules_preview_rejects_cross_origin():
    with web.app.test_client() as client:
        resp = client.post(
            "/api/noise-rules/preview",
            json={"tool": "claude-code", "role": "user", "content": "hi"},
            headers={"Origin": "http://evil.example.com"},
        )
    assert resp.status_code == 403


def test_action_cancel_rejects_cross_origin():
    with web.app.test_client() as client:
        resp = client.post(
            "/api/action-cancel/some-job-id",
            headers={"Origin": "http://evil.example.com"},
        )
    assert resp.status_code == 403


def test_noise_rules_preview_allows_same_origin():
    """A same-origin request passes the guard (reaches normal handling)."""
    with web.app.test_client() as client:
        resp = client.post(
            "/api/noise-rules/preview",
            json={"tool": "claude-code", "role": "user", "content": "hi"},
            headers={"Origin": "http://localhost"},
        )
    # Not a 403 — the guard let it through (200/400 depending on payload).
    assert resp.status_code != 403
