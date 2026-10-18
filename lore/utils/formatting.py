"""Message formatting utilities for automatic import-time cleanup.

This module provides the MessageFormatter class which automatically cleans
tool-specific artifacts, detects code blocks, and formats messages for display.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from markupsafe import Markup, escape


class CodeLanguage(Enum):
    """Supported programming languages for syntax highlighting."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    BASH = "bash"
    SHELL = "shell"
    YAML = "yaml"
    JSON = "json"
    MARKDOWN = "markdown"
    SQL = "sql"
    RUST = "rust"
    GO = "go"
    JAVA = "java"
    CPP = "cpp"
    C = "c"
    HTML = "html"
    CSS = "css"
    SCSS = "scss"
    DOCKERFILE = "dockerfile"
    TOML = "toml"
    INI = "ini"
    XML = "xml"
    REGEX = "regex"
    PLAINTEXT = "text"


@dataclass
class CodeBlock:
    """Represents a detected code block with metadata."""

    language: str
    content: str
    start_pos: int
    end_pos: int
    confidence: float


class MessageFormatter:
    """Automatically formats messages during import.

    This class handles:
    1. Tool-specific artifact cleaning
    2. Code block detection and language labeling
    3. Markdown normalization
    4. Tool output summarization

    Usage:
        formatter = MessageFormatter()
        clean_content = formatter.format_message(content, tool="claude")
    """

    # Language detection patterns for code blocks
    LANGUAGE_PATTERNS: Dict[CodeLanguage, List[str]] = {
        CodeLanguage.PYTHON: [
            r"^\s*(def |class |import |from |if __name__|print\(|#.*python)",
            r"\b(pip install|python -m|\.py\b)",
        ],
        CodeLanguage.JAVASCRIPT: [
            r"^\s*(const |let |var |function |=> |import |export |console\.)",
            r"\b(node |npm |yarn |\.js\b)",
        ],
        CodeLanguage.TYPESCRIPT: [
            r"^\s*(interface |type |enum |namespace |declare |\.tsx?)",
            r"\b(npx ts-|tsc |\.ts\b)",
        ],
        CodeLanguage.BASH: [
            r"^\s*(#!/bin/bash|#!/bin/sh|#!/usr/bin/env bash)",
            r"\b(sudo |apt |yum |brew |curl |wget |chmod |chown |ls |cd |pwd |echo |export |source )",
        ],
        CodeLanguage.SHELL: [
            r"^\s*(#!/bin/bash|#!/bin/sh|#!/usr/bin/env sh)",
            r"\$\w+",
        ],
        CodeLanguage.YAML: [
            r"^\s*---\s*$",
            r"^\s*[\w-]+:\s*(\w+|\[|\{)",
            r"\.ya?ml\b",
        ],
        CodeLanguage.JSON: [
            r"^\s*[\{\[]",
            r'"[\w-]+"\s*:\s*"[^"]*"',
            r"\.json\b",
        ],
        CodeLanguage.SQL: [
            r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|FROM|WHERE|JOIN)\b",
            r"\.sql\b",
        ],
        CodeLanguage.RUST: [
            r"\b(fn |let |mut |impl |struct |enum |match |use |mod |cargo)",
            r"\.rs\b",
        ],
        CodeLanguage.GO: [
            r"\b(package |import |func |go |chan |defer |goroutine)",
            r"\.go\b",
        ],
        CodeLanguage.JAVA: [
            r"\b(public |private |class |interface |extends |implements |void |String )",
            r"\.java\b",
        ],
        CodeLanguage.CPP: [
            r"\b(#include|using namespace|std::|cout <<)",
            r"\.(cpp|hpp|h)\b",
        ],
        CodeLanguage.C: [
            r"\b(#include|printf|scanf|malloc|free|struct \w+ \{)",
            r"\.c\b",
        ],
        CodeLanguage.HTML: [
            r"<(!DOCTYPE|html|head|body|div|script|style|link)",
            r"\.html?\b",
        ],
        CodeLanguage.CSS: [
            r"[.#]\w+\s*\{",
            r"\b(color|background|margin|padding|font|display):",
            r"\.css\b",
        ],
        CodeLanguage.DOCKERFILE: [
            r"^\s*(FROM|RUN|CMD|LABEL|MAINTAINER|EXPOSE|ENV|ADD|COPY|ENTRYPOINT|VOLUME|USER|WORKDIR|ARG)",
            r"Dockerfile",
        ],
        CodeLanguage.MARKDOWN: [
            r"^\s*(#{1,6} |\*|\-|\d+\. |\[.*?\]\(.*?\))",
            r"\.md\b",
        ],
    }

    def __init__(self):
        """Initialize the formatter with compiled regex patterns."""
        self._compiled_patterns: Dict[str, List[Any]] = {}
        self._compile_patterns()

    def _compile_patterns(self):
        """Compile regex patterns for performance."""
        # Tool-specific artifact patterns to clean
        raw_patterns: Dict[str, List[Any]] = {
            "claude": [
                (r"<thinking>.*?</thinking>", "", re.DOTALL),
                (r"\[thinking\].*?\[/thinking\]", "", re.DOTALL),
            ],
            "cursor": [
                (r"```cursor\n", "```\n"),
            ],
            "gemini": [
                (r"\$\$[0-9a-fA-F-]+\$\$", ""),
            ],
            "codex": [
                (r"\[CODEX-[0-9]+\]\s*", ""),
            ],
            "copilot": [
                (r"\[GITHUB-COPILOT\]\s*", ""),
            ],
            "opencode": [
                (r"\[OPENCODE-[0-9]+\]\s*", ""),
            ],
            "antigravity": [
                (r"\[ANTIGRAVITY-[0-9]+\]\s*", ""),
            ],
            "vscode": [
                (r"\[VSCODE-[0-9]+\]\s*", ""),
            ],
        }

        for tool, patterns in raw_patterns.items():
            compiled = []
            for pattern_data in patterns:
                if len(pattern_data) == 2:
                    pattern, replacement = pattern_data
                    flags = 0
                else:
                    pattern, replacement, flags = pattern_data
                try:
                    compiled.append((re.compile(pattern, flags), replacement))
                except re.error as e:
                    print(f"Warning: Failed to compile pattern '{pattern}' for {tool}: {e}")
            self._compiled_patterns[tool] = compiled

    def format_message(self, content: str, tool: Optional[str] = None) -> str:
        """Format a message by cleaning artifacts and normalizing content.

        Args:
            content: The raw message content
            tool: Optional tool name for tool-specific cleaning

        Returns:
            Cleaned and formatted content
        """
        if not content:
            return content

        content = self._clean_artifacts(content, tool)
        content = self._format_code_blocks(content)
        content = self._normalize_markdown(content)
        content = self._clean_whitespace(content)

        return content

    def _clean_artifacts(self, content: str, tool: Optional[str]) -> str:
        """Remove tool-specific artifacts from content."""
        if tool and tool in self._compiled_patterns:
            for pattern, replacement in self._compiled_patterns[tool]:
                content = pattern.sub(replacement, content)

        if tool and tool not in self._compiled_patterns:
            for tool_name, patterns in self._compiled_patterns.items():
                for pattern, replacement in patterns:
                    content = pattern.sub(replacement, content)

        return content

    def _format_code_blocks(self, content: str) -> str:
        """Detect code blocks, label languages, and clean formatting."""
        code_block_pattern = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)

        def format_block(match: re.Match) -> str:
            lang = match.group(1) or ""
            code = match.group(2)

            if not lang:
                lang = self._detect_language(code)

            code = code.rstrip()

            if lang:
                return f"```{lang}\n{code}\n```"
            return f"```\n{code}\n```"

        return code_block_pattern.sub(format_block, content)

    def _detect_language(self, code: str) -> str:
        """Detect the programming language of a code snippet.

        Args:
            code: The code snippet to analyze

        Returns:
            Language identifier string (or empty if unknown)
        """
        code_start = code[:500]
        scores: Dict[CodeLanguage, int] = {}

        for lang, patterns in self.LANGUAGE_PATTERNS.items():
            score = 0
            for pattern in patterns:
                if re.search(pattern, code_start, re.IGNORECASE | re.MULTILINE):
                    score += 1
            if score > 0:
                scores[lang] = score

        if not scores:
            return ""

        best_lang = max(scores.items(), key=lambda x: x[1])
        return best_lang[0].value if best_lang[1] >= 1 else ""

    def _normalize_markdown(self, content: str) -> str:
        """Normalize markdown formatting."""
        content = re.sub(r"^(#{1,6})([^\s#])", r"\1 \2", content, flags=re.MULTILINE)
        content = re.sub(r"^\s*[-*]\s*", "- ", content, flags=re.MULTILINE)
        return content

    def _clean_whitespace(self, content: str) -> str:
        """Clean up excessive whitespace while preserving structure."""
        content = re.sub(r"[ \t]+$", "", content, flags=re.MULTILINE)
        parts = re.split(r"(```[\s\S]*?```)", content)

        for i, part in enumerate(parts):
            if not part.startswith("```"):
                part = re.sub(r"\n{4,}", "\n\n\n", part)
                parts[i] = part

        content = "".join(parts)
        content = content.strip()
        return content

    def detect_code_blocks(self, content: str) -> List[CodeBlock]:
        """Detect and extract all code blocks from content.

        Args:
            content: The message content to analyze

        Returns:
            List of CodeBlock objects with metadata
        """
        blocks = []
        pattern = re.compile(r"```(\w+)?\n(.*?)```", re.DOTALL)

        for match in pattern.finditer(content):
            lang = match.group(1) or ""
            code = match.group(2)

            if not lang:
                lang = self._detect_language(code)

            confidence = 0.8 if match.group(1) else 0.6

            blocks.append(
                CodeBlock(
                    language=lang or "text",
                    content=code,
                    start_pos=match.start(),
                    end_pos=match.end(),
                    confidence=confidence,
                )
            )

        return blocks

    def format_tool_output(self, output: str, tool_name: str, max_lines: int = 50) -> str:
        """Format tool output with optional summarization.

        Args:
            output: The raw tool output
            tool_name: Name of the tool that produced output
            max_lines: Maximum lines before truncation

        Returns:
            Formatted output (possibly truncated with summary)
        """
        if not output:
            return output

        lines = output.split("\n")

        if len(lines) <= max_lines:
            return self.format_message(output, tool=tool_name)

        truncated = lines[:max_lines]
        remaining = len(lines) - max_lines
        summary = f"\n\n... ({remaining} more lines)"
        formatted = "\n".join(truncated) + summary
        return self.format_message(formatted, tool=tool_name)


_formatter = None


def get_formatter() -> MessageFormatter:
    """Get the global MessageFormatter instance."""
    global _formatter
    if _formatter is None:
        _formatter = MessageFormatter()
    return _formatter


def format_message(content: str, tool: Optional[str] = None) -> str:
    """Convenience function to format a message.

    Args:
        content: The raw message content
        tool: Optional tool name for tool-specific cleaning

    Returns:
        Cleaned and formatted content
    """
    return get_formatter().format_message(content, tool)


def format_tool_output(output: str, tool_name: str, max_lines: int = 50) -> str:
    """Convenience function to format tool output.

    Args:
        output: The raw tool output
        tool_name: Name of the tool that produced output
        max_lines: Maximum lines before truncation

    Returns:
        Formatted output (possibly truncated)
    """
    return get_formatter().format_tool_output(output, tool_name, max_lines)


def escape_snippet_html(text: str) -> Markup:
    """Render a ``«``/``»``-marked snippet as safe HTML with ``<mark>`` highlights.

    Escapes first, then substitutes the marks, so untrusted text can never add
    markup of its own. ``snippet_to_text`` has already neutralised literal
    ``«``/``»`` in the source text.
    """
    escaped = str(escape(text or ""))
    return Markup(escaped.replace("«", "<mark>").replace("»", "</mark>"))  # nosec B704
