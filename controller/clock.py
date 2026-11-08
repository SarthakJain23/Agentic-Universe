"""Simulated time helpers. Minute 0 == D0 00:00 == 2027-01-04T00:00:00+05:30 (from the API contract)."""
from __future__ import annotations

import datetime as dt

MIN_PER_DAY = 1440
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
D0 = dt.datetime(2027, 1, 4, tzinfo=_IST)

_ts_cache: dict[str, int] = {}


def ts_to_minute(ts: str) -> int:
    """'2027-01-05T09:03:00+05:30' -> 1983. Cached: a chunk has only a handful of distinct timestamps."""
    m = _ts_cache.get(ts)
    if m is None:
        m = int((dt.datetime.fromisoformat(ts) - D0).total_seconds() // 60)
        if len(_ts_cache) > 200_000:
            _ts_cache.clear()
        _ts_cache[ts] = m
    return m


def day_of(minute: int) -> int:
    return minute // MIN_PER_DAY


def hour_of(minute: int) -> int:
    return (minute % MIN_PER_DAY) // 60


def label(minute: int) -> str:
    """1983 -> 'D1 09:03'. Negative minutes are history ('D-3 10:00')."""
    d, rem = divmod(minute, MIN_PER_DAY)
    return f"D{d} {rem // 60:02d}:{rem % 60:02d}"


def parse_label(s: str) -> int:
    """'D3 14:00' -> minute. Also accepts a bare integer string."""
    s = s.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    day, hm = s.split()
    h, m = hm.split(":")
    return int(day.upper().lstrip("D")) * MIN_PER_DAY + int(h) * 60 + int(m)


def in_hours(minute: int, hours: list[int] | tuple[int, int]) -> bool:
    lo, hi = hours
    return lo <= hour_of(minute) < hi


def next_open(minute: int, hours: list[int] | tuple[int, int]) -> int:
    """Earliest minute >= `minute` inside [lo, hi) hours."""
    lo, hi = hours
    d, rem = divmod(minute, MIN_PER_DAY)
    if rem < lo * 60:
        return d * MIN_PER_DAY + lo * 60
    if rem >= hi * 60:
        return (d + 1) * MIN_PER_DAY + lo * 60
    return minute
