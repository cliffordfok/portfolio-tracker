"""Dependency-free New York market time helpers."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


NEW_YORK_NAME = "America/New_York"
_NYSE_EARLY_CLOSES = {
    date(2026, 11, 27),
    date(2026, 12, 24),
    date(2027, 11, 26),
    date(2028, 7, 3),
    date(2028, 11, 24),
}


def _nth_weekday(
    year: int,
    month: int,
    weekday: int,
    occurrence: int,
) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (occurrence - 1) * 7)


def _dst_bounds_utc(year: int) -> tuple[datetime, datetime]:
    start_day = _nth_weekday(year, 3, 6, 2)
    end_day = _nth_weekday(year, 11, 6, 1)
    return (
        datetime.combine(start_day, time(7), tzinfo=UTC),
        datetime.combine(end_day, time(6), tzinfo=UTC),
    )


def new_york_local(utc_value: datetime) -> datetime:
    """Convert an aware datetime to New York with a Windows-safe fallback."""

    if utc_value.tzinfo is None or utc_value.utcoffset() is None:
        raise ValueError("UTC value must include a timezone")
    normalized = utc_value.astimezone(UTC)
    try:
        return normalized.astimezone(ZoneInfo(NEW_YORK_NAME))
    except ZoneInfoNotFoundError:
        dst_start, dst_end = _dst_bounds_utc(normalized.year)
        hours = -4 if dst_start <= normalized < dst_end else -5
        offset = timedelta(hours=hours)
        return (normalized + offset).replace(
            tzinfo=timezone(offset, name=NEW_YORK_NAME),
        )


def is_nyse_early_close(session: date) -> bool:
    """Return official NYSE 13:00 close dates published for 2026-2028."""

    return session in _NYSE_EARLY_CLOSES


def new_york_close_utc(session: date) -> datetime:
    """Return the official New York close in UTC for a supported session."""

    close_time = time(13) if is_nyse_early_close(session) else time(16)
    try:
        local = datetime.combine(
            session,
            close_time,
            tzinfo=ZoneInfo(NEW_YORK_NAME),
        )
        return local.astimezone(UTC)
    except ZoneInfoNotFoundError:
        dst_start = _nth_weekday(session.year, 3, 6, 2)
        dst_end = _nth_weekday(session.year, 11, 6, 1)
        offset = timedelta(
            hours=-4 if dst_start <= session < dst_end else -5
        )
        local = datetime.combine(
            session,
            close_time,
            tzinfo=timezone(offset, name=NEW_YORK_NAME),
        )
        return local.astimezone(UTC)
