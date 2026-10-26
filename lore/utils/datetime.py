import re
from datetime import datetime, time, timedelta, tzinfo

# The archive stores naive timestamps in the host's local wall clock, so every
# filter boundary is expressed in that same frame. A date-only end value means
# the whole selected day, not its midnight.
END_OF_DAY = time(23, 59, 59, 999999)


def parse_duration(duration_str: str) -> timedelta:
    """Parse duration string like '7d', '2w', '1m'."""
    match = re.match(r"^(\d+)([dwmh])$", duration_str.lower())
    if not match:
        raise ValueError(f"Invalid duration format: {duration_str}")

    value = int(match.group(1))
    unit = match.group(2)

    if unit == "h":
        return timedelta(hours=value)
    elif unit == "d":
        return timedelta(days=value)
    elif unit == "w":
        return timedelta(weeks=value)
    elif unit == "m":
        return timedelta(days=value * 30)

    return timedelta(days=value)


def make_naive(dt: datetime) -> datetime:
    """Convert datetime to naive (no timezone) for comparison.

    This only drops the ``tzinfo``; use :func:`to_local_naive` when an aware
    value must be converted to the host's wall clock first.
    """
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def to_local_naive(dt: datetime) -> datetime:
    """Convert to the host's local wall clock without tzinfo (the storage frame).

    A naive value is already local wall clock and stays untouched; an aware
    value is converted, so ``2026-09-12T18:00:00+00:00`` and
    ``2026-09-12T20:00:00+02:00`` become the same local instant.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone().replace(tzinfo=None)


def parse_date_bound(value, *, end: bool = False, tz: tzinfo | None = None) -> datetime | None:
    """Parse a date or timestamp filter value into a naive local boundary.

    ``2026-09-12`` with ``end=False`` is that day's first microsecond, with
    ``end=True`` its last one, so ``created <= end`` keeps every session of the
    selected final day (the historical bug dropped it). A timestamp keeps its
    time of day; one carrying an offset is converted to the local wall clock,
    and a naive one is taken as already local. An optional ``tz`` anchors
    date-only values in that timezone instead of the host's: the returned value
    is still local wall clock, matching what the archive stores.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None

    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            day = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        boundary = datetime.combine(day.date(), END_OF_DAY) if end else day
        if tz is not None:
            return boundary.replace(tzinfo=tz).astimezone().replace(tzinfo=None)
        return boundary

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None and tz is not None:
        parsed = parsed.replace(tzinfo=tz)
    return to_local_naive(parsed)


def datetime_in_range(
    value: datetime, start: datetime | None = None, end: datetime | None = None
) -> bool:
    """Whether ``value`` lies inside the inclusive ``[start, end]`` window.

    Both bounds are boundaries produced by :func:`parse_date_bound`, so an
    end-of-day bound includes the session stored at 23:59:59 of that day.
    """
    if start is not None and value < start:
        return False
    if end is not None and value > end:
        return False
    return True


def parse_timestamp(ts) -> datetime:
    """Parse timestamp (can be string, int, or float)."""
    if isinstance(ts, (int, float)):
        # Handle milliseconds if huge number
        if ts > 1e12:
            ts = ts / 1000
        return datetime.fromtimestamp(ts)

    try:
        ts_str = str(ts).strip()

        # Handle "2025-11-24 21:38:42.296187599" (Space separator)
        if " " in ts_str and "T" not in ts_str:
            ts_str = ts_str.replace(" ", "T")

        # Handle "Z" suffix
        ts_str = ts_str.replace("Z", "+00:00")

        # Truncate microseconds if too long (Python only supports 6 digits)
        if "." in ts_str:
            parts = ts_str.split(".")
            if len(parts) > 1 and len(parts[1]) > 6:
                # Keep first 6 digits of microseconds/nanoseconds
                # Need to handle timezone offset if present
                micro_part = parts[1]
                plus_pos = micro_part.find("+")
                minus_pos = micro_part.find("-")

                tz_suffix = ""
                if plus_pos != -1:
                    tz_suffix = micro_part[plus_pos:]
                    micro_part = micro_part[:plus_pos]
                elif minus_pos != -1:
                    tz_suffix = micro_part[minus_pos:]
                    micro_part = micro_part[:minus_pos]

                if len(micro_part) > 6:
                    micro_part = micro_part[:6]

                ts_str = f"{parts[0]}.{micro_part}{tz_suffix}"

        return datetime.fromisoformat(ts_str)
    except Exception:
        return datetime.fromtimestamp(0)
