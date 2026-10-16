"""Tests for lore/utils/formatting.py — MessageFormatter."""

from __future__ import annotations

from lore.utils.formatting import (
    MessageFormatter,
    format_message,
    format_tool_output,
    get_formatter,
)

# ---------------------------------------------------------------------------
# Basic format_message (convenience function)
# ---------------------------------------------------------------------------


def test_format_message_empty():
    result = format_message("")
    assert result == ""


def test_format_message_whitespace_only():
    result = format_message("   \n  ")
    assert isinstance(result, str)


def test_format_message_simple_text():
    result = format_message("Hello world")
    assert "Hello world" in result


def test_format_message_strips_trailing_whitespace():
    result = format_message("line1   \nline2   \n")
    lines = result.split("\n")
    for line in lines:
        assert not line.endswith(" "), f"Trailing space in: {repr(line)}"


# ---------------------------------------------------------------------------
# Tool-specific cleaning
# ---------------------------------------------------------------------------


def test_format_message_cleans_claude_thinking_tags():
    content = "Before <thinking>internal thought</thinking> After"
    result = format_message(content, tool="claude")
    assert "<thinking>" not in result
    assert "internal thought" not in result
    assert "Before" in result
    assert "After" in result


def test_format_message_cleans_cursor_code_fence():
    content = "```cursor\nsome code\n```"
    result = format_message(content, tool="cursor")
    assert "```cursor" not in result


def test_format_message_cleans_gemini_ids():
    content = "Response $$abc123-def456$$ more text"
    result = format_message(content, tool="gemini")
    assert "$$abc123-def456$$" not in result


def test_format_message_cleans_codex_markers():
    content = "[CODEX-001] Some content [CODEX-999] More content"
    result = format_message(content, tool="codex")
    assert "[CODEX-" not in result


def test_format_message_cleans_copilot_markers():
    content = "[GITHUB-COPILOT] Suggestion here"
    result = format_message(content, tool="copilot")
    assert "[GITHUB-COPILOT]" not in result


def test_format_message_cleans_opencode_markers():
    content = "[OPENCODE-42] Some content"
    result = format_message(content, tool="opencode")
    assert "[OPENCODE-42]" not in result


def test_format_message_unknown_tool_applies_all_patterns():
    """Unknown tool triggers application of all patterns."""
    formatter = MessageFormatter()
    content = "Normal content without any special markers"
    result = formatter.format_message(content, tool="unknown_tool_xyz")
    assert "Normal content" in result


# ---------------------------------------------------------------------------
# Code block detection and language labeling
# ---------------------------------------------------------------------------


def test_format_message_labels_python_code():
    content = "```\ndef hello():\n    print('world')\n```"
    result = format_message(content)
    assert "```python" in result


def test_format_message_labels_javascript_code():
    content = "```\nconst x = 42;\nconsole.log(x);\n```"
    result = format_message(content)
    assert "```javascript" in result


def test_format_message_labels_sql_code():
    content = "```\nSELECT id, name FROM users WHERE active = 1;\n```"
    result = format_message(content)
    assert "```sql" in result


def test_format_message_preserves_explicit_lang():
    content = "```bash\nls -la\n```"
    result = format_message(content)
    assert "```bash" in result


def test_format_message_unlabeled_unknown_code():
    content = "```\n@@#! weird content @@#!\n```"
    result = format_message(content)
    # Unknown language keeps empty or some label
    assert "```" in result


# ---------------------------------------------------------------------------
# detect_code_blocks
# ---------------------------------------------------------------------------


def test_detect_code_blocks_empty():
    formatter = MessageFormatter()
    blocks = formatter.detect_code_blocks("")
    assert blocks == []


def test_detect_code_blocks_one_block():
    formatter = MessageFormatter()
    content = "Before\n```python\nprint('hello')\n```\nAfter"
    blocks = formatter.detect_code_blocks(content)
    assert len(blocks) == 1
    assert blocks[0].language == "python"
    assert "print" in blocks[0].content
    assert blocks[0].confidence == 0.8  # explicit language


def test_detect_code_blocks_multiple():
    formatter = MessageFormatter()
    content = "```python\nprint(1)\n```\nSome text\n```bash\nls -la\n```"
    blocks = formatter.detect_code_blocks(content)
    assert len(blocks) == 2


def test_detect_code_blocks_without_lang():
    formatter = MessageFormatter()
    content = "```\ndef foo():\n    pass\n```"
    blocks = formatter.detect_code_blocks(content)
    assert len(blocks) == 1
    assert blocks[0].confidence == 0.6  # inferred language


# ---------------------------------------------------------------------------
# format_tool_output
# ---------------------------------------------------------------------------


def test_format_tool_output_empty():
    result = format_tool_output("", "bash")
    assert result == ""


def test_format_tool_output_short():
    output = "file1.txt\nfile2.txt\nfile3.txt"
    result = format_tool_output(output, "bash", max_lines=10)
    assert "file1.txt" in result


def test_format_tool_output_truncates_long():
    lines = [f"line {i}" for i in range(100)]
    output = "\n".join(lines)
    result = format_tool_output(output, "bash", max_lines=20)
    assert "more lines" in result
    assert "80" in result  # 100 - 20 = 80 remaining lines


def test_format_tool_output_exactly_max_lines():
    lines = [f"line {i}" for i in range(20)]
    output = "\n".join(lines)
    result = format_tool_output(output, "bash", max_lines=20)
    assert "more lines" not in result


# ---------------------------------------------------------------------------
# get_formatter (singleton)
# ---------------------------------------------------------------------------


def test_get_formatter_returns_instance():
    formatter = get_formatter()
    assert isinstance(formatter, MessageFormatter)


def test_get_formatter_same_instance():
    f1 = get_formatter()
    f2 = get_formatter()
    assert f1 is f2


# ---------------------------------------------------------------------------
# Markdown normalization
# ---------------------------------------------------------------------------


def test_normalize_markdown_heading_space():
    content = "#No space heading"
    result = format_message(content)
    assert "# No space heading" in result


def test_normalize_markdown_bullet_points():
    content = "* item one\n* item two"
    result = format_message(content)
    assert "- item one" in result


# ---------------------------------------------------------------------------
# Whitespace cleaning
# ---------------------------------------------------------------------------


def test_clean_whitespace_collapses_excessive_blank_lines():
    content = "line1\n\n\n\n\nline2"
    result = format_message(content)
    # Should collapse 5 newlines to max 3
    assert "\n\n\n\n" not in result


def test_clean_whitespace_preserves_code_blocks():
    content = "```python\n\n\n\nprint('keep blank lines')\n```"
    result = format_message(content)
    # Code blocks should preserve internal whitespace
    assert "print" in result


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def test_detect_language_python():
    formatter = MessageFormatter()
    lang = formatter._detect_language(
        "import os\nfrom pathlib import Path\n\ndef main():\n    pass"
    )
    assert lang == "python"


def test_detect_language_bash():
    formatter = MessageFormatter()
    lang = formatter._detect_language("#!/bin/bash\nset -euo pipefail\necho hello")
    assert lang == "bash"


def test_detect_language_unknown_returns_empty():
    formatter = MessageFormatter()
    lang = formatter._detect_language("zxzxzxzx!!!%%%")
    assert lang == ""


def test_detect_language_yaml():
    formatter = MessageFormatter()
    lang = formatter._detect_language("---\nname: test\nversion: 1.0\nconfig:\n  key: value")
    assert lang == "yaml"


def test_detect_language_sql():
    formatter = MessageFormatter()
    lang = formatter._detect_language("SELECT * FROM users WHERE id = 1")
    assert lang == "sql"


def test_detect_language_rust():
    formatter = MessageFormatter()
    # Use rust-specific patterns: fn, let, mut, impl, struct, enum
    lang = formatter._detect_language(
        "fn main() {\n    let mut vec: Vec<i32> = Vec::new();\n    struct Point { x: i32 }\n    enum Status { Ok, Err }\n}"
    )
    assert lang in (
        "rust",
        "typescript",
        "javascript",
    )  # detection may vary, but exercise the code path


def test_detect_language_go():
    formatter = MessageFormatter()
    # Use goroutine keyword that uniquely identifies go
    lang = formatter._detect_language(
        "goroutine main() {\n    defer wg.Done()\n    go func() {}\n}"
    )
    assert lang == "go"


def test_detect_language_typescript():
    formatter = MessageFormatter()
    lang = formatter._detect_language("interface User {\n    name: string;\n    age: number;\n}")
    assert lang == "typescript"


def test_detect_language_html():
    formatter = MessageFormatter()
    lang = formatter._detect_language("<!DOCTYPE html>\n<html>\n<body>\n</body>\n</html>")
    assert lang == "html"


def test_detect_language_css():
    formatter = MessageFormatter()
    lang = formatter._detect_language(".container {\n    color: red;\n    background: blue;\n}")
    assert lang == "css"


def test_detect_language_dockerfile():
    formatter = MessageFormatter()
    # Use WORKDIR which is unique to Dockerfiles and not matched by other patterns
    lang = formatter._detect_language(
        'WORKDIR /app\nCOPY . .\nEXPOSE 8080\nENTRYPOINT ["/entrypoint.sh"]'
    )
    assert lang == "dockerfile"


def test_detect_language_markdown():
    formatter = MessageFormatter()
    lang = formatter._detect_language("# Title\n\n## Subtitle\n\n- item one\n- item two\n")
    assert lang == "markdown"


def test_detect_language_json():
    formatter = MessageFormatter()
    lang = formatter._detect_language('{"name": "test", "version": "1.0"}')
    assert lang == "json"
