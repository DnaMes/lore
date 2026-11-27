"""Tests for Edit/Write diff rendering (annuli/utils/text_processing.format_diff)."""

from __future__ import annotations

import json

from annuli.utils.text_processing import _diff_rows, format_diff, format_tool_display


def test_diff_rows_basic_add_and_delete():
    old = "a\nb\nc"
    new = "a\nB\nc"
    rows, adds, dels = _diff_rows(old, new)
    assert adds == 1
    assert dels == 1
    kinds = [r["kind"] for r in rows]
    assert "add" in kinds and "del" in kinds and "context" in kinds


def test_diff_rows_pure_addition():
    rows, adds, dels = _diff_rows("a\nb", "a\nb\nc")
    assert adds == 1
    assert dels == 0


def test_diff_rows_collapses_long_unchanged_runs():
    old = "\n".join(f"line{i}" for i in range(40))
    new = old + "\nNEW LAST"
    rows, adds, dels = _diff_rows(old, new)
    # A gap row stands in for the bulk of the unchanged middle.
    assert any(r["kind"] == "gap" for r in rows)
    assert adds == 1


def test_format_diff_edit_renders_block_and_stats():
    html = format_diff("auth.py", "old line", "new line")
    assert "diff-block" in html
    assert "diff-file" in html and "auth.py" in html
    assert "diff-stat-add" in html and "diff-stat-del" in html
    assert "diff-add" in html and "diff-del" in html


def test_format_diff_new_file_all_additions():
    html = format_diff("new.py", "", "l1\nl2\nl3", is_new_file=True)
    assert html.count("diff-add") >= 3
    assert "diff-del" not in html  # no deletions on a brand-new file
    assert "+3" in html


def test_format_diff_escapes_html():
    html = format_diff("x", "", "<script>alert(1)</script>", is_new_file=True)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_format_tool_display_edit_routes_to_diff():
    args = json.dumps(
        {
            "file_path": "auth.py",
            "old_string": "user = get_user(token)",
            "new_string": "token = req.headers['x']\nuser = get_user(token)",
            "replace_all": False,
        }
    )
    out = format_tool_display("Edit", args)
    assert "diff-block" in out
    # kv-grid is suppressed when a diff renders
    assert "action-kv-grid" not in out


def test_format_tool_display_write_routes_to_new_file_diff():
    args = json.dumps({"file_path": "new.py", "content": "print('hi')\nprint('bye')"})
    out = format_tool_display("Write", args)
    assert "diff-block" in out
    assert "diff-del" not in out
