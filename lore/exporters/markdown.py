from pathlib import Path

from ..core.models import Role, UnifiedSession
from ..utils.paths import project_to_dirname, sanitize_filename
from .tool_calls import canonical_tool_calls, tool_call_markdown


class MarkdownExporter:
    """Export sessions to Markdown format."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir

    def _candidate_path(self, session: UnifiedSession) -> Path:
        """Return the output path for a session without writing anything."""
        project_dir = project_to_dirname(session.project_path)
        tool_dir = session.tool.value
        date_str = session.created_at.strftime("%Y-%m-%d")
        title_part = sanitize_filename(session.title or session.session_id[:8])
        session_suffix = sanitize_filename(session.session_id)[-12:]
        filename = f"{date_str}_{title_part}_{session_suffix}.md"
        return self.output_dir / "projects" / project_dir / tool_dir / filename

    def export_session(self, session: UnifiedSession, force: bool = False) -> Path:
        """Export a single session to Markdown. Skips if file is up-to-date."""
        # Build output path
        project_dir = project_to_dirname(session.project_path)
        tool_dir = session.tool.value

        output_path = self.output_dir / "projects" / project_dir / tool_dir
        output_path.mkdir(parents=True, exist_ok=True)

        # Generate filename
        date_str = session.created_at.strftime("%Y-%m-%d")
        title_part = sanitize_filename(session.title or session.session_id[:8])
        session_suffix = sanitize_filename(session.session_id)[-12:]
        filename = f"{date_str}_{title_part}_{session_suffix}.md"

        file_path = output_path / filename

        # Skip if file exists and session hasn't been updated since last export
        if not force and file_path.exists():
            file_mtime = file_path.stat().st_mtime
            session_updated_ts = session.last_updated.timestamp()
            if file_mtime >= session_updated_ts:
                return file_path

        # Generate content
        content = self._generate_markdown(session)

        import os

        tmp = file_path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, file_path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        return file_path

    def _generate_markdown(self, session: UnifiedSession) -> str:
        """Generate Markdown content for a session."""
        lines = []

        # Frontmatter
        lines.append("---")
        lines.append(f"tool: {session.tool.value}")
        lines.append(f"session_id: {session.session_id}")
        if session.project_path:
            lines.append(f"project: {session.project_path}")
        if session.thread_id:
            lines.append(f"thread_id: {session.thread_id}")
        if session.title_source is not None:
            lines.append(
                f"title_source: {getattr(session.title_source, 'value', session.title_source)}"
            )
        lines.append(f"created: {session.created_at.isoformat()}")
        lines.append(f"updated: {session.last_updated.isoformat()}")
        lines.append(f"messages: {session.message_count}")
        if session.total_tokens:
            lines.append(f"tokens: {session.total_tokens}")
        if session.cli_version:
            lines.append(f"cli_version: {session.cli_version}")
        if session.git_branch:
            lines.append(f"git_branch: {session.git_branch}")
        if getattr(session, "git_commit", None):
            lines.append(f"git_commit: {session.git_commit}")
        lines.append("---")
        lines.append("")

        # Title
        title = session.title or session.summary or f"Session {session.session_id[:8]}"
        lines.append(f"# {title}")
        lines.append("")
        # (#165) Titles and summaries are derived: say so instead of letting a
        # generated string pass as captured transcript content.
        title_source = getattr(session.title_source, "value", session.title_source)
        if title_source and title_source != "native":
            lines.append(f"*Title source: {title_source} (derived, not transcript content).*")
            lines.append("")
        if session.summary:
            lines.append("**Summary (derived, not transcript content):**")
            lines.append("")
            lines.append(session.summary)
            lines.append("")

        # Metadata
        lines.append(f"**Tool:** {session.tool.value}")
        if session.cli_version:
            lines.append(f" v{session.cli_version}")
        lines.append("")
        if session.project_path:
            lines.append(f"**Project:** `{session.project_path}`")
            lines.append("")

        # Duration
        duration = session.last_updated - session.created_at
        if duration.total_seconds() > 60:
            minutes = int(duration.total_seconds() / 60)
            hours = minutes // 60
            mins = minutes % 60
            if hours > 0:
                lines.append(f"**Duration:** {hours}h {mins}m")
            else:
                lines.append(f"**Duration:** {mins}m")
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append("## Conversation")
        lines.append("")

        # Messages
        if not session.messages:
            lines.append("_No messages were captured for this session._")
            lines.append("")
        for msg in session.messages:
            role_display = msg.role.value.title()
            time_str = msg.timestamp.strftime("%H:%M:%S")

            lines.append(f"### {role_display} ({time_str})")
            lines.append("")

            if msg.role == Role.USER:
                # Quote user messages
                for line in msg.content.split("\n"):
                    lines.append(f"> {line}")
            else:
                lines.append(msg.content)

            # Tool calls and events, canonical envelope (#166): name, identity,
            # arguments, result/error, status and truncation, in one shape.
            calls = canonical_tool_calls(msg)
            if calls:
                lines.append("")
                for call in calls:
                    lines.extend(tool_call_markdown(call))
                    lines.append("")

            # Reasoning/thoughts
            if msg.reasoning:
                lines.append("")
                lines.append("<details>")
                lines.append("<summary>Reasoning</summary>")
                lines.append("")
                lines.append(msg.reasoning)
                lines.append("</details>")

            lines.append("")
            lines.append("---")
            lines.append("")

        # Statistics
        if session.message_count > 0:
            lines.append("## Statistics")
            lines.append("")
            lines.append("| Metric | Value |")
            lines.append("|--------|-------|")
            lines.append(f"| Total Messages | {session.message_count} |")

            user_msgs = sum(1 for m in session.messages if m.role == Role.USER)
            asst_msgs = sum(1 for m in session.messages if m.role == Role.ASSISTANT)
            lines.append(f"| User Messages | {user_msgs} |")
            lines.append(f"| Assistant Messages | {asst_msgs} |")

            tool_calls = sum(len(m.tool_calls) for m in session.messages)
            if tool_calls > 0:
                lines.append(f"| Tool Calls | {tool_calls} |")

            if session.total_tokens:
                lines.append(f"| Total Tokens | ~{session.total_tokens:,} |")

        return "\n".join(lines)
