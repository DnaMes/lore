from datetime import datetime

from annuli.core.models import Tool, UnifiedSession
from annuli.exporters.markdown import MarkdownExporter


def _session(session_id: str, title: str) -> UnifiedSession:
    now = datetime(2026, 3, 4, 12, 0, 0)
    return UnifiedSession(
        tool=Tool.CODEX,
        session_id=session_id,
        created_at=now,
        last_updated=now,
        messages=[],
        title=title,
    )


def test_export_uses_unique_filename_suffix(tmp_path):
    exporter = MarkdownExporter(tmp_path)

    p1 = exporter.export_session(_session("ses_alpha_111111", "same-title"))
    p2 = exporter.export_session(_session("ses_beta_222222", "same-title"))

    assert p1 != p2
    assert p1.exists()
    assert p2.exists()
