"""First-run UX: skip summaries, missing-index message, search output, semantic hint."""

from __future__ import annotations

import argparse
import builtins
import logging

import pytest

import lore_cli
from lore.storage import embeddings


class _FakeExtractor:
    def __init__(self, skip_counts: dict[str, int], min_prompts: int = 3) -> None:
        self.skip_counts = skip_counts
        self._min_prompts = min_prompts

    def _effective_thresholds(self) -> tuple[int, int, int]:
        return (1, 0, self._min_prompts)


def test_skip_summary_explains_too_few_prompts(capsys):
    lore_cli._print_skip_summary([_FakeExtractor({"too_few_user_prompts": 2})])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        "Skipped 2 sessions with fewer than 3 user prompts "
        "(set LORE_MIN_USER_PROMPTS=1 to include them)"
    ) in captured.err


def test_skip_summary_sums_reasons_across_extractors(capsys):
    extractors = [
        _FakeExtractor({"too_few_user_prompts": 1, "parse_error": 1}),
        _FakeExtractor({"too_few_user_prompts": 1, "too_little_content": 3}),
    ]
    lore_cli._print_skip_summary(extractors)
    err = capsys.readouterr().err
    assert "Skipped 2 sessions with fewer than 3 user prompts" in err
    assert "Skipped 3 sessions with too little content" in err
    assert "Skipped 1 (parse error)" in err


def test_skip_summary_silent_when_nothing_skipped(capsys):
    lore_cli._print_skip_summary([_FakeExtractor({}), _FakeExtractor({"x": 0})])
    assert capsys.readouterr().err == ""


def test_search_without_index_prints_stats_message(tmp_path, capsys):
    args = argparse.Namespace(
        query="anything",
        output_dir=str(tmp_path),
        tool=None,
        project=None,
        include_automated=False,
    )
    rc = lore_cli.cmd_search(args)
    assert rc is None
    assert capsys.readouterr().out.strip() == "No index found. Run 'lore export --all' first."


def _search_args(tmp_path, query="q"):
    (tmp_path / "index.json").write_text("{}", encoding="utf-8")
    return argparse.Namespace(
        query=query,
        output_dir=str(tmp_path),
        tool=None,
        project=None,
        include_automated=False,
    )


def _hit(score: float) -> list[dict]:
    session = {
        "tool": "claude-code",
        "title": "Hit",
        "id": "abc123",
        "created": "2026-01-01",
        "messages": 3,
    }
    return [{"session": session, "score": score}]


def test_search_singular_result_and_rounded_score(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("lore.services.index.search_index", lambda *a, **k: _hit(-1.63e-06))
    lore_cli.cmd_search(_search_args(tmp_path))
    out = capsys.readouterr().out
    assert "Found 1 result for 'q':" in out
    assert "Score: -0.000 |" in out
    assert "e-06" not in out


def test_search_plural_results_keeps_three_decimals(tmp_path, capsys, monkeypatch):
    hits = _hit(0.123456) * 2
    monkeypatch.setattr("lore.services.index.search_index", lambda *a, **k: hits)
    lore_cli.cmd_search(_search_args(tmp_path))
    out = capsys.readouterr().out
    assert "Found 2 results for 'q':" in out
    assert out.count("Score: 0.123 |") == 2


def test_semantic_unavailable_hint_when_fastembed_missing(monkeypatch, caplog):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fastembed":
            raise ImportError("No module named 'fastembed'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_model_failed", False)
    with caplog.at_level(logging.WARNING, logger=embeddings.logger.name):
        assert embeddings._get_model() is None
    message = caplog.records[-1].getMessage()
    assert message.startswith(
        "Embedding model unavailable, semantic search disabled: No module named 'fastembed'"
    )
    assert "pipx inject lore fastembed sqlite-vec" in message


@pytest.mark.parametrize("exc", [RuntimeError("boom")])
def test_semantic_hint_only_for_import_errors(monkeypatch, caplog, exc):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "fastembed":
            raise exc
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_model_failed", False)
    with caplog.at_level(logging.WARNING, logger=embeddings.logger.name):
        assert embeddings._get_model() is None
    assert "pip install" not in caplog.records[-1].getMessage()
