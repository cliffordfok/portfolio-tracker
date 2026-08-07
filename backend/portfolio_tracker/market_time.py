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


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _new_year_holiday(year: int) -> date:
    day = date(year, 1, 1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    # NYSE does not observe a Saturday New Year's Day on the preceding Friday
    # because that Friday is the final monthly and yearly accounting period.
    return day


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Return Gregorian Easter for the NYSE Good Friday rule."""

    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


_SPECIAL_NYSE_CLOSURES = {
    date(2001, 9, 11),
    date(2001, 9, 12),
    date(2001, 9, 13),
    date(2001, 9, 14),
    date(2004, 6, 11),
    date(2007, 1, 2),
    date(2012, 10, 29),
    date(2012, 10, 30),
    date(2018, 12, 5),
    date(2025, 1, 9),
}


def _nyse_holidays(year: int) -> set[date]:
    holidays = {
        _new_year_holiday(year),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    holidays.update(day for day in _SPECIAL_NYSE_CLOSURES if day.year == year)
    return holidays


def is_nyse_session(day: date) -> bool:
    """Return whether a calendar day is an NYSE trading session."""

    return day.weekday() < 5 and day not in _nyse_holidays(day.year)


def nyse_sessions(start: date, end: date) -> list[str]:
    """Return inclusive NYSE session dates in ISO format."""

    if end < start:
        return []
    sessions: list[str] = []
    cursor = start
    while cursor <= end:
        if is_nyse_session(cursor):
            sessions.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return sessions


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


def latest_completed_nyse_session(now: datetime) -> date:
    """Return the latest session whose official close has passed."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    normalized = now.astimezone(UTC)
    candidate = new_york_local(normalized).date()
    if (
        not is_nyse_session(candidate)
        or normalized < new_york_close_utc(candidate)
    ):
        candidate -= timedelta(days=1)
    while not is_nyse_session(candidate):
        candidate -= timedelta(days=1)
    return candidate
