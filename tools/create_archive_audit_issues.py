#!/usr/bin/env python3
"""Create the issue-ready entries from the durable archive audit.

The command is deliberately dry-run by default. ``--apply`` is required for
GitHub writes, and exact-title checks make retries idempotent. The audit report
remains the source of truth for issue bodies.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_REPOSITORY = "DnaMes/lore"
DEFAULT_REPORT = (
    Path(__file__).resolve().parents[1] / "docs/audits/2026-09-12-lore-archive-audit.md"
)
ISSUE_HEADING = re.compile(r"^## (?P<number>\d{2}) — (?P<title>.+)$", re.MULTILINE)


def parse_issues(report: str) -> list[dict[str, str]]:
    """Parse numbered issue sections from the audit report."""
    matches = list(ISSUE_HEADING.finditer(report))
    issues: list[dict[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(report)
        body = report[match.end() : end].strip()
        issues.append(
            {
                "number": match.group("number"),
                "title": f"Archive #{match.group('number')}: {match.group('title').strip()}",
                "body": body,
            }
        )
    return issues


def existing_titles(repository: str) -> dict[str, int]:
    """Return all existing issue titles and their numbers."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "all",
            "--limit",
            "1000",
            "--json",
            "number,title",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    records: list[dict[str, Any]] = json.loads(result.stdout)
    return {str(record["title"]): int(record["number"]) for record in records}


def create_issue(repository: str, title: str, body: str) -> str:
    """Create one GitHub issue and return its URL."""
    result = subprocess.run(
        ["gh", "issue", "create", "--repo", repository, "--title", title, "--body", body],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    """Parse the audit and optionally create missing GitHub issues."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true", help="create missing GitHub issues")
    args = parser.parse_args()

    issues = parse_issues(args.report.read_text(encoding="utf-8"))
    if len(issues) != 44:
        raise SystemExit(f"expected 44 issue sections, found {len(issues)}")

    titles = existing_titles(args.repository)
    action = "create" if args.apply else "would create"
    for issue in issues:
        existing = titles.get(issue["title"])
        if existing is not None:
            print(f"skip #{existing}: {issue['title']}")
            continue
        if args.apply:
            url = create_issue(args.repository, issue["title"], issue["body"])
            print(f"created {url}")
        else:
            print(f"{action}: {issue['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
