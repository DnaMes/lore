"""Date filter semantics shared by the web, CLI, API and MCP surfaces (#172).

The archive stores naive timestamps in the host's local wall clock. Filters
therefore have to speak the same frame: a date-only end value covers the whole
selected day, an offset-carrying timestamp is converted instead of raising the
naive/aware TypeError, and every surface reuses the same boundary helper.

The tables are literal: each row names the input, the expected boundary and why
it matters, so a regression points at the exact semantic that broke.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from lore.interfaces.web_helpers import filter_sessions, parse_date_param
from lore.utils.datetime import (
    datetime_in_range,
    parse_date_bound,
    to_local_naive,
)


@pytest.fixture()
def berlin(monkeypatch):
    """Run a test in Europe/Berlin so local wall clock is deterministic."""
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@pytest.fixture()
def new_york(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("not-a-date", None),
        ("2026-09-12", datetime(2026, 9, 12, 0, 0, 0)),
        ("2026/09/12", datetime(2026, 9, 12, 0, 0, 0)),
        ("2026-09-12T10:30:00", datetime(2026, 9, 12, 10, 30, 0)),
    ],
)
def test_start_boundary_table(berlin, value, expected):
    assert parse_date_bound(value) == expected


def test_end_boundary_covers_the_whole_day(berlin):
    assert parse_date_bound("2026-09-12", end=True) == datetime(2026, 9, 12, 23, 59, 59, 999999)


def test_date_only_end_keeps_the_selected_final_day(berlin):
    """The historical bug: `created > end` dropped everything on the last day."""
    sessions = [
        {"id": "early", "created": "2026-09-12T00:00:00"},
        {"id": "evening", "created": "2026-09-12T18:00:00"},
        {"id": "last-second", "created": "2026-09-12T23:59:59"},
        {"id": "next-day", "created": "2026-09-13T00:00:01"},
    ]
    kept = [s["id"] for s in filter_sessions(sessions, end=parse_date_param("2026-09-12", end=True))]
    assert kept == ["early", "evening", "last-second"]


def test_offset_timestamps_are_converted_not_compared_naively(berlin):
    """An aware `created` used to raise TypeError against a naive boundary."""
    sessions = [
        {"id": "utc-midnight", "created": "2026-09-11T22:30:00+00:00"},  # 00:30 local
        {"id": "before", "created": "2026-09-11T21:30:00+00:00"},  # 23:30 local, previous day
        {"id": "zulu", "created": "2026-09-12T12:00:00Z"},
    ]
    kept = [s["id"] for s in filter_sessions(sessions, start=parse_date_param("2026-09-12"))]
    assert kept == ["utc-midnight", "zulu"]


def test_naive_legacy_values_are_taken_as_local(berlin):
    sessions = [{"id": "legacy", "created": "2026-09-12T23:30:00"}]
    assert filter_sessions(sessions, end=parse_date_param("2026-09-12", end=True))
    assert filter_sessions(sessions, start=parse_date_param("2026-09-12"))


def test_dst_spring_forward_day_is_covered_including_the_late_hour(new_york):
    """2026-03-08 in New York skips 02:00-03:00; the day still ends at 23:59:59."""
    sessions = [
        {"id": "after-jump", "created": "2026-03-08T03:30:00"},
        {"id": "late", "created": "2026-03-08T23:59:59"},
        {"id": "next", "created": "2026-03-09T00:00:00"},
    ]
    kept = [
        s["id"] for s in filter_sessions(sessions, end=parse_date_param("2026-03-08", end=True))
    ]
    assert kept == ["after-jump", "late"]


def test_dst_fall_back_hour_is_a_single_wall_clock_window(new_york):
    """2026-11-01 repeats 01:00-02:00 locally; the naive store keeps one timestamp."""
    sessions = [
        {"id": "repeated-hour", "created": "2026-11-01T01:30:00"},
        {"id": "next-day", "created": "2026-11-02T00:00:00"},
    ]
    kept = [
        s["id"]
        for s in filter_sessions(
            sessions,
            start=parse_date_param("2026-11-01"),
            end=parse_date_param("2026-11-01", end=True),
        )
    ]
    assert kept == ["repeated-hour"]


def test_explicit_timezone_anchors_a_date_only_value(berlin):
    """Midnight in UTC is 02:00 in Berlin, so a UTC day starts two hours later."""
    bound = parse_date_bound("2026-09-12", tz=timezone.utc)
    assert bound == datetime(2026, 9, 12, 2, 0, 0)


def test_explicit_timezone_applies_to_naive_timestamps_as_well(berlin):
    bound = parse_date_bound("2026-09-12T00:00:00", tz=timezone.utc)
    assert bound == datetime(2026, 9, 12, 2, 0, 0)


def test_aware_timestamp_is_converted_to_local_wall_clock(berlin):
    assert to_local_naive(datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc)) == datetime(
        2026, 9, 12, 20, 0
    )
    assert to_local_naive(datetime(2026, 9, 12, 18, 0)) == datetime(2026, 9, 12, 18, 0)


@pytest.mark.parametrize(
    "created,start,end,expected",
    [
        (datetime(2026, 9, 12, 0, 0), datetime(2026, 9, 12, 0, 0), None, True),
        (datetime(2026, 9, 11, 23, 59), datetime(2026, 9, 12, 0, 0), None, False),
        (datetime(2026, 9, 12, 23, 59, 59, 999999), None, datetime(2026, 9, 12, 23, 59, 59, 999999), True),
        (datetime(2026, 9, 13, 0, 0), None, datetime(2026, 9, 12, 23, 59, 59, 999999), False),
        (datetime(2026, 9, 12, 12, 0), None, None, True),
    ],
)
def test_inclusive_range_table(created, start, end, expected):
    assert datetime_in_range(created, start, end) is expected


def test_cli_and_mcp_cutoffs_share_the_local_frame(berlin):
    """A `--since 24h` cutoff and an offset-carrying `created` agree."""
    from lore.interfaces.mcp_tools.search import to_local_naive as mcp_normalizer

    cutoff = datetime.now() - timedelta(hours=24)
    created_aware = (datetime.now(timezone.utc) - timedelta(hours=1)).astimezone()
    assert mcp_normalizer(created_aware) >= cutoff
    assert datetime_in_range(to_local_naive(created_aware), start=cutoff)
