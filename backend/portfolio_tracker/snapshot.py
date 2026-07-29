"""Build the public, derived dashboard snapshot from private master ledgers."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .decimal_utils import ZERO, amount_for, json_safe, money, percent, price, shares
from .errors import BusinessInvariantError, ValidationError
from .ledger import FileLock, LedgerStore, atomic_write_json, durable_unlink
from .replay import ReplayResult, replay_portfolio
from .schemas import (
    INSTRUMENT_ID_RE,
    INSTRUMENT_TYPES,
    SYMBOL_RE,
    parse_timestamp,
)

try:
    NEW_YORK: ZoneInfo | None = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    NEW_YORK = None
MARKET_CLOSE = time(16, 0)


def _snapshot_decimal_or_none(value: Any, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a Decimal string or null")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError(
            f"{label} must be a Decimal string or null"
        ) from exc
    if not parsed.is_finite():
        raise ValidationError(f"{label} must be finite")


def _snapshot_date(value: Any, *, label: str) -> None:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValidationError(f"{label} must use YYYY-MM-DD")


def _snapshot_timestamp(
    value: Any,
    *,
    label: str,
    nullable: bool = True,
) -> None:
    if value is None and nullable:
        return
    try:
        parse_timestamp(value, field=label)
    except (ValidationError, ValueError) as exc:
        raise ValidationError(
            f"{label} must be a UTC timestamp or null"
        ) from exc


def validate_snapshot(snapshot: Any) -> None:
    """Validate the complete intrinsic schema of one public snapshot."""

    if not isinstance(snapshot, dict):
        raise ValidationError("portfolio snapshot must be a JSON object")
    if snapshot.get("schema_version") != 4:
        raise ValidationError("portfolio snapshot schema_version must be 4")
    revision = snapshot.get("revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ValidationError(
            "portfolio snapshot revision must be a non-negative integer"
        )
    if snapshot.get("currency") != "USD":
        raise ValidationError("portfolio snapshot currency must be USD")

    source_head = snapshot.get("source_head")
    if (
        not isinstance(source_head, dict)
        or set(source_head) != {"paper", "live", "market"}
    ):
        raise ValidationError(
            "portfolio snapshot source_head must contain paper, live, and market"
        )
    source_count = 0
    for name in ("paper", "live", "market"):
        head = source_head[name]
        if not isinstance(head, dict):
            raise ValidationError(f"source_head.{name} must be an object")
        count = head.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValidationError(
                f"source_head.{name}.count must be a non-negative integer"
            )
        digest = head.get("hash")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError(
                f"source_head.{name}.hash must be a lowercase SHA-256 digest"
            )
        last_event_id = head.get("last_event_id")
        if count == 0:
            if last_event_id is not None:
                raise ValidationError(
                    f"source_head.{name}.last_event_id must be null when count is zero"
                )
        elif (
            not isinstance(last_event_id, str)
            or not last_event_id.startswith(f"{name}-")
        ):
            raise ValidationError(
                f"source_head.{name}.last_event_id is invalid"
            )
        source_count += count
    if revision != source_count:
        raise ValidationError(
            "portfolio snapshot revision does not match source_head counts"
        )

    _snapshot_timestamp(
        snapshot.get("generated_at"),
        label="snapshot.generated_at",
        nullable=False,
    )
    for field in ("data_as_of", "prices_as_of"):
        if field not in snapshot:
            raise ValidationError(f"portfolio snapshot is missing {field}")
        _snapshot_timestamp(snapshot[field], label=f"snapshot.{field}")
    warnings = snapshot.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        raise ValidationError("portfolio snapshot warnings must be strings")

    portfolios = snapshot.get("portfolios")
    if (
        not isinstance(portfolios, dict)
        or set(portfolios) != {"paper", "live"}
    ):
        raise ValidationError(
            "portfolio snapshot portfolios must contain paper and live"
        )
    for name in ("paper", "live"):
        portfolio = portfolios[name]
        if not isinstance(portfolio, dict):
            raise ValidationError(
                f"portfolio snapshot has no {name} object"
            )
        if portfolio.get("data_status") not in {
            "OK",
            "INSUFFICIENT_DATA",
            "NO_DATA",
        }:
            raise ValidationError(f"{name} data_status is invalid")
        for field in ("holdings", "recent_trades", "daily"):
            if not isinstance(portfolio.get(field), list):
                raise ValidationError(f"{name}.{field} must be an array")
        for field in ("cash", "initial_cash"):
            if field in portfolio:
                _snapshot_decimal_or_none(
                    portfolio[field],
                    label=f"{name}.{field}",
                )

        for holding in portfolio["holdings"]:
            if not isinstance(holding, dict):
                raise ValidationError(
                    f"{name}.holdings entries must be objects"
                )
            if (
                not isinstance(holding.get("symbol"), str)
                or not SYMBOL_RE.fullmatch(holding["symbol"])
            ):
                raise ValidationError(f"{name}.holdings symbol is invalid")
            if (
                not isinstance(holding.get("instrument_id"), str)
                or not INSTRUMENT_ID_RE.fullmatch(holding["instrument_id"])
            ):
                raise ValidationError(
                    f"{name}.holdings instrument_id is invalid"
                )
            if holding.get("instrument_type") not in INSTRUMENT_TYPES:
                raise ValidationError(
                    f"{name}.holdings instrument_type is invalid"
                )
            instrument_name = holding.get("instrument_name")
            if instrument_name is not None and not isinstance(
                instrument_name,
                str,
            ):
                raise ValidationError(
                    f"{name}.holdings instrument_name is invalid"
                )
            quote_symbol = holding.get("quote_symbol")
            if quote_symbol is not None and (
                not isinstance(quote_symbol, str)
                or not SYMBOL_RE.fullmatch(quote_symbol)
            ):
                raise ValidationError(
                    f"{name}.holdings quote_symbol is invalid"
                )
            if holding.get("quote_status") not in {
                "OK",
                "MANUAL",
                "MISSING",
            }:
                raise ValidationError(
                    f"{name}.holdings quote_status is invalid"
                )
            for field in (
                "shares",
                "avg_cost",
                "cost_basis",
                "contract_multiplier",
                "current_price",
                "market_value",
                "unrealized_pnl",
                "unrealized_pnl_pct",
            ):
                if field not in holding:
                    raise ValidationError(
                        f"{name}.holdings is missing {field}"
                    )
                _snapshot_decimal_or_none(
                    holding[field],
                    label=f"{name}.holdings.{field}",
                )
            if "market_price_as_of" not in holding:
                raise ValidationError(
                    f"{name}.holdings is missing market_price_as_of"
                )
            _snapshot_timestamp(
                holding["market_price_as_of"],
                label=f"{name}.holdings.market_price_as_of",
            )

        for trade in portfolio["recent_trades"]:
            if not isinstance(trade, dict) or trade.get("action") not in {
                "BUY",
                "SELL",
                "CASH_FLOW",
                "INCOME_EXPENSE",
                "SPLIT",
            }:
                raise ValidationError(
                    f"{name}.recent_trades entry is invalid"
                )
            if not isinstance(trade.get("event_id"), str) or not trade["event_id"]:
                raise ValidationError(
                    f"{name}.recent_trades event_id is invalid"
                )
            if trade.get("portfolio") != name:
                raise ValidationError(
                    f"{name}.recent_trades contains a cross-portfolio event"
                )
            if not isinstance(trade.get("source"), str) or not trade["source"]:
                raise ValidationError(
                    f"{name}.recent_trades source is invalid"
                )
            ledger_seq = trade.get("ledger_seq")
            if (
                isinstance(ledger_seq, bool)
                or not isinstance(ledger_seq, int)
                or ledger_seq < 1
            ):
                raise ValidationError(
                    f"{name}.recent_trades ledger_seq is invalid"
                )
            _snapshot_timestamp(
                trade.get("occurred_at"),
                label=f"{name}.recent_trades.occurred_at",
                nullable=False,
            )
            _snapshot_timestamp(
                trade.get("created_at"),
                label=f"{name}.recent_trades.created_at",
                nullable=False,
            )
            if trade["action"] in {"BUY", "SELL"}:
                if (
                    not isinstance(trade.get("symbol"), str)
                    or not SYMBOL_RE.fullmatch(trade["symbol"])
                ):
                    raise ValidationError(
                        f"{name}.recent_trades symbol is invalid"
                    )
                for field in (
                    "shares",
                    "price",
                    "fee",
                    "pnl",
                    "pnl_pct",
                ):
                    if field not in trade:
                        raise ValidationError(
                            f"{name}.recent_trades is missing {field}"
                        )
                    _snapshot_decimal_or_none(
                        trade[field],
                        label=f"{name}.recent_trades.{field}",
                    )
                if "settlement_adjustment" in trade:
                    _snapshot_decimal_or_none(
                        trade["settlement_adjustment"],
                        label=(
                            f"{name}.recent_trades."
                            "settlement_adjustment"
                        ),
                    )
            elif trade["action"] == "CASH_FLOW" and trade.get("symbol") != "USD":
                raise ValidationError(
                    "CASH_FLOW snapshot symbol must be USD"
                )
            elif trade["action"] == "CASH_FLOW":
                if "amount" not in trade:
                    raise ValidationError(
                        f"{name}.recent_trades is missing amount"
                    )
                _snapshot_decimal_or_none(
                    trade["amount"],
                    label=f"{name}.recent_trades.amount",
                )
            elif trade["action"] == "INCOME_EXPENSE":
                if trade.get("income_type") not in {
                    "DIVIDEND",
                    "INTEREST",
                    "FEE",
                    "CASH_IN_LIEU",
                    "OTHER",
                }:
                    raise ValidationError(
                        f"{name}.recent_trades income_type is invalid"
                    )
                for field in (
                    "amount",
                    "gross_amount",
                    "withholding_tax",
                    "pnl",
                    "pnl_pct",
                ):
                    if field not in trade:
                        raise ValidationError(
                            f"{name}.recent_trades is missing {field}"
                        )
                    _snapshot_decimal_or_none(
                        trade[field],
                        label=f"{name}.recent_trades.{field}",
                    )
            elif trade["action"] == "SPLIT":
                for field in (
                    "numerator",
                    "denominator",
                    "shares_before",
                    "shares_after",
                    "pnl",
                    "pnl_pct",
                ):
                    if field not in trade:
                        raise ValidationError(
                            f"{name}.recent_trades is missing {field}"
                        )
                    _snapshot_decimal_or_none(
                        trade[field],
                        label=f"{name}.recent_trades.{field}",
                    )

        for point in portfolio["daily"]:
            if not isinstance(point, dict):
                raise ValidationError(
                    f"{name}.daily entries must be objects"
                )
            _snapshot_date(point.get("date"), label=f"{name}.daily.date")
            if point.get("data_status") not in {
                "OK",
                "INSUFFICIENT_DATA",
                "INSUFFICIENT_MARKET_DATA",
            }:
                raise ValidationError(f"{name}.daily data_status is invalid")
            for field in (
                "nav",
                "cash",
                "external_flow",
                "daily_return",
                "cumulative_return",
                "segment_return",
                "pnl",
            ):
                if field not in point:
                    raise ValidationError(f"{name}.daily is missing {field}")
                _snapshot_decimal_or_none(
                    point[field],
                    label=f"{name}.daily.{field}",
                )
            missing_symbols = point.get("missing_symbols")
            if not isinstance(missing_symbols, list) or not all(
                isinstance(symbol, str) and SYMBOL_RE.fullmatch(symbol)
                for symbol in missing_symbols
            ):
                raise ValidationError(
                    f"{name}.daily missing_symbols is invalid"
                )
            segment_id = point.get("segment_id")
            if not (
                segment_id is None
                or (
                    not isinstance(segment_id, bool)
                    and isinstance(segment_id, int)
                    and segment_id > 0
                )
            ):
                raise ValidationError(
                    f"{name}.daily segment_id is invalid"
                )

        metrics = portfolio.get("metrics")
        if not isinstance(metrics, dict):
            raise ValidationError(f"{name}.metrics must be an object")
        if metrics.get("data_status") not in {
            "OK",
            "INSUFFICIENT_DATA",
            "NO_DATA",
        }:
            raise ValidationError(f"{name}.metrics data_status is invalid")
        for field in (
            "total_return",
            "realized_pnl",
            "income_expense",
            "win_rate",
            "max_drawdown",
            "sharpe_ratio",
        ):
            if field not in metrics:
                raise ValidationError(
                    f"{name}.metrics is missing {field}"
                )
            _snapshot_decimal_or_none(
                metrics[field],
                label=f"{name}.metrics.{field}",
            )
        closed_episodes = metrics.get("closed_episodes")
        if (
            isinstance(closed_episodes, bool)
            or not isinstance(closed_episodes, int)
            or closed_episodes < 0
        ):
            raise ValidationError(
                f"{name}.metrics.closed_episodes is invalid"
            )
        has_effective_date = "performance_effective_date" in metrics
        has_performance_scope = "performance_scope" in metrics
        if has_effective_date != has_performance_scope:
            raise ValidationError(
                f"{name}.metrics performance metadata is incomplete"
            )
        if has_effective_date:
            effective_date = metrics["performance_effective_date"]
            performance_scope = metrics["performance_scope"]
            if effective_date is not None:
                _snapshot_date(
                    effective_date,
                    label=f"{name}.metrics.performance_effective_date",
                )
            if performance_scope not in {
                None,
                "FULL_HISTORY",
                "LATEST_COMPLETE_SEGMENT",
            }:
                raise ValidationError(
                    f"{name}.metrics.performance_scope is invalid"
                )
            if (effective_date is None) != (performance_scope is None):
                raise ValidationError(
                    f"{name}.metrics performance metadata is inconsistent"
                )
            if metrics["data_status"] == "OK" and effective_date is None:
                raise ValidationError(
                    f"{name}.metrics OK status requires an effective date"
                )

    benchmark = snapshot.get("benchmark")
    if not isinstance(benchmark, dict) or benchmark.get("symbol") != "SPY":
        raise ValidationError("portfolio snapshot benchmark must be SPY")
    daily = benchmark.get("daily")
    if not isinstance(daily, list):
        raise ValidationError(
            "portfolio snapshot benchmark.daily must be an array"
        )
    for point in daily:
        if not isinstance(point, dict):
            raise ValidationError(
                "benchmark.daily entries must be objects"
            )
        _snapshot_date(point.get("date"), label="benchmark.daily.date")
        if point.get("data_status") not in {
            "OK",
            "INSUFFICIENT_MARKET_DATA",
        }:
            raise ValidationError("benchmark.daily data_status is invalid")
        for field in (
            "close",
            "daily_return",
            "cumulative_return",
            "segment_return",
        ):
            if field not in point:
                raise ValidationError(f"benchmark.daily is missing {field}")
            _snapshot_decimal_or_none(
                point[field],
                label=f"benchmark.daily.{field}",
            )
        segment_id = point.get("segment_id")
        if not (
            segment_id is None
            or (
                not isinstance(segment_id, bool)
                and isinstance(segment_id, int)
                and segment_id > 0
            )
        ):
            raise ValidationError("benchmark.daily segment_id is invalid")


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    """Gregorian Easter, used to derive the NYSE Good Friday closure."""

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
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    next_new_year_observed = _observed(date(year + 1, 1, 1))
    if next_new_year_observed.year == year:
        holidays.add(next_new_year_observed)
    holidays.update(day for day in _SPECIAL_NYSE_CLOSURES if day.year == year)
    return holidays


def is_nyse_session(day: date) -> bool:
    return day.weekday() < 5 and day not in _nyse_holidays(day.year)


def nyse_sessions(start: date, end: date) -> list[str]:
    if end < start:
        return []
    sessions: list[str] = []
    cursor = start
    while cursor <= end:
        if is_nyse_session(cursor):
            sessions.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return sessions


def _calendar_days(start: date, end: date) -> list[str]:
    days: list[str] = []
    cursor = start
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (occurrence - 1) * 7)


def _new_york_local(utc_value: datetime) -> datetime:
    """Convert UTC to America/New_York without requiring the tzdata wheel.

    The fallback implements the post-2007 US DST rules used by every ledger
    date supported by this project. Linux VPS hosts normally use ZoneInfo;
    the fallback keeps Windows development deterministic and dependency-free.
    """

    if NEW_YORK is not None:
        return utc_value.astimezone(NEW_YORK)
    year = utc_value.year
    dst_start_day = _nth_weekday(year, 3, 6, 2)  # second Sunday in March
    dst_end_day = _nth_weekday(year, 11, 6, 1)  # first Sunday in November
    dst_start_utc = datetime.combine(dst_start_day, time(7), tzinfo=UTC)
    dst_end_utc = datetime.combine(dst_end_day, time(6), tzinfo=UTC)
    offset = timedelta(hours=-4 if dst_start_utc <= utc_value < dst_end_utc else -5)
    return (utc_value + offset).replace(tzinfo=None)


def _source_head(path: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    raw = path.read_bytes() if path.exists() else b""
    return {
        "count": len(events),
        "last_event_id": events[-1]["event_id"] if events else None,
        "hash": hashlib.sha256(raw).hexdigest(),
    }


def _market_data(
    market_events: list[dict[str, Any]],
) -> tuple[
    dict[str, dict[str, dict[str, Any]]],
    dict[str, dict[str, Any]],
    list[str],
]:
    quotes: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    benchmark: dict[str, dict[str, Any]] = {}
    for event in market_events:
        session = event["session_date"]
        record = {
            "close": price(event["close"], field="close"),
            "market_price_as_of": event["occurred_at"],
            "source": event["source"],
        }
        if event["action"] == "QUOTE":
            quote_key = event.get("instrument_id") or event["symbol"]
            quotes[quote_key][session] = record
        elif event["action"] == "BENCHMARK_CLOSE":
            benchmark[session] = record
    recorded_sessions = sorted(
        set(benchmark) | {day for values in quotes.values() for day in values}
    )
    if recorded_sessions:
        sessions = nyse_sessions(
            date.fromisoformat(recorded_sessions[0]),
            date.fromisoformat(recorded_sessions[-1]),
        )
    else:
        sessions = []
    return dict(quotes), benchmark, sessions


def _assert_pending_batch_complete(
    root: Path,
    events: list[dict[str, Any]],
) -> None:
    """Never rebuild a snapshot from a partially durable ledger batch."""

    marker_path = root / "state" / "rebuild.pending"
    if not marker_path.exists():
        return
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessInvariantError("rebuild.pending is invalid") from exc
    if (
        not isinstance(marker, dict)
        or marker.get("requested_by") != "ledger-append-batch"
    ):
        return
    expected = marker.get("event_ids")
    if (
        not isinstance(expected, list)
        or not expected
        or not all(isinstance(event_id, str) for event_id in expected)
    ):
        raise BusinessInvariantError("ledger batch marker is invalid")
    durable_ids = {event["event_id"] for event in events}
    missing = [event_id for event_id in expected if event_id not in durable_ids]
    if missing:
        raise BusinessInvariantError(
            "incomplete ledger batch; retry the original stable event IDs: "
            + ", ".join(missing)
        )


def _quote_for_day(
    records: dict[str, dict[str, Any]],
    day: str,
    trading_sessions: set[str],
) -> dict[str, Any] | None:
    if day in trading_sessions:
        return records.get(day)
    eligible = [session for session in records if session < day]
    return records[max(eligible)] if eligible else None


def _event_session_candidate(event: dict[str, Any]) -> str:
    occurred = parse_timestamp(event["occurred_at"], field="occurred_at")
    local = _new_york_local(occurred)
    session_day = local.date()
    if (
        local.timetz().replace(tzinfo=None) > MARKET_CLOSE
        or not is_nyse_session(session_day)
    ):
        session_day += timedelta(days=1)
        while not is_nyse_session(session_day):
            session_day += timedelta(days=1)
    return session_day.isoformat()


def _session_for_event(event: dict[str, Any], sessions: list[str]) -> str | None:
    if not sessions:
        return None
    session_text = _event_session_candidate(event)
    index = bisect_left(sessions, session_text)
    if index >= len(sessions) or sessions[index] != session_text:
        return None
    return sessions[index]


def _latest_completed_session(now: datetime) -> date:
    local = _new_york_local(now.astimezone(UTC))
    candidate = local.date()
    if (
        not is_nyse_session(candidate)
        or local.timetz().replace(tzinfo=None) < MARKET_CLOSE
    ):
        candidate -= timedelta(days=1)
    while not is_nyse_session(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _position_quote_key(item: dict[str, Any]) -> str:
    """Resolve a quote identity without reusing an underlying for derivatives.

    Early imported OPTION/PRIVATE events carried a display-oriented
    ``quote_symbol``. Treating that alias as a price source materially
    misprices the instrument, so these identities always quote by their stable
    instrument ID. Listed equities and ETFs may continue to use ticker aliases.
    """

    instrument_id = item.get("instrument_id") or item["symbol"]
    if item.get("instrument_type") in {"OPTION", "PRIVATE"}:
        return instrument_id
    return item.get("quote_symbol") or instrument_id


def _enrich_holdings(
    result: ReplayResult,
    quotes: dict[str, dict[str, dict[str, Any]]],
    last_day: str | None,
    trading_sessions: set[str],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for holding in result.holdings:
        symbol = holding["symbol"]
        quote_key = _position_quote_key(holding)
        current_quote = (
            _quote_for_day(
                quotes.get(quote_key, {}),
                last_day,
                trading_sessions,
            )
            if last_day
            else None
        )
        current_price = current_quote["close"] if current_quote else None
        market_value = (
            amount_for(
                holding["shares"] * holding["contract_multiplier"],
                current_price,
            )
            if current_price is not None
            else None
        )
        unrealized = (
            money(market_value - holding["cost_basis"])
            if market_value is not None
            else None
        )
        enriched.append(
            {
                **holding,
                "current_price": current_price,
                "market_price_as_of": (
                    current_quote["market_price_as_of"] if current_quote else None
                ),
                "quote_status": (
                    "MANUAL"
                    if current_quote
                    and current_quote["source"] == "manual-quote"
                    else "OK"
                    if current_quote
                    else "MISSING"
                ),
                "market_value": market_value,
                "unrealized_pnl": unrealized,
                "unrealized_pnl_pct": (
                    percent(unrealized / holding["cost_basis"])
                    if unrealized is not None and holding["cost_basis"] != 0
                    else None
                ),
            }
        )
    return enriched


def _metrics(
    daily: list[dict[str, Any]], result: ReplayResult
) -> dict[str, Any]:
    performance_daily: list[dict[str, Any]] = []
    performance_scope: str | None = None
    performance_effective_date: str | None = None

    if daily and daily[-1]["data_status"] == "OK":
        latest_segment_id = daily[-1].get("segment_id")
        if (
            not isinstance(latest_segment_id, bool)
            and isinstance(latest_segment_id, int)
            and latest_segment_id > 0
        ):
            for item in reversed(daily):
                if (
                    item["data_status"] != "OK"
                    or item.get("segment_id") != latest_segment_id
                ):
                    break
                performance_daily.append(item)
            performance_daily.reverse()
        elif all(
            item["data_status"] == "OK"
            and item.get("cumulative_return") is not None
            for item in daily
        ):
            # Compatibility for callers that provide the pre-segmentation
            # in-memory shape. Generated snapshots always contain segment_id.
            performance_daily = list(daily)

    if performance_daily:
        performance_effective_date = performance_daily[0]["date"]
        performance_scope = (
            "FULL_HISTORY"
            if len(performance_daily) == len(daily)
            else "LATEST_COMPLETE_SEGMENT"
        )

    valid_returns = [
        item["daily_return"]
        for item in performance_daily
        if (
            item["daily_return"] is not None
            and is_nyse_session(date.fromisoformat(item["date"]))
        )
    ]
    total_return = (
        performance_daily[-1].get("segment_return")
        if performance_daily
        else None
    )
    if total_return is None and performance_daily:
        total_return = performance_daily[-1].get("cumulative_return")
    max_drawdown: Decimal | None = None
    sharpe: Decimal | None = None

    if performance_daily:
        peak = Decimal("-1")
        drawdowns: list[Decimal] = []
        for item in performance_daily:
            cumulative = item.get("segment_return")
            if cumulative is None:
                cumulative = item.get("cumulative_return")
            if cumulative is None:
                continue
            peak = max(peak, cumulative)
            drawdowns.append(percent((1 + cumulative) / (1 + peak) - 1))
        max_drawdown = min(drawdowns) if drawdowns else ZERO
        if len(valid_returns) >= 20:
            count = Decimal(len(valid_returns))
            mean = sum(valid_returns, ZERO) / count
            variance = (
                sum(((value - mean) ** 2 for value in valid_returns), ZERO)
                / Decimal(len(valid_returns) - 1)
            )
            deviation = variance.sqrt()
            if deviation:
                annualized = mean / deviation * Decimal(252).sqrt()
                sharpe = percent(annualized)

    has_performance = bool(performance_daily)
    return {
        "data_status": "OK" if has_performance else "INSUFFICIENT_DATA",
        "performance_effective_date": performance_effective_date,
        "performance_scope": performance_scope,
        "total_return": total_return if has_performance else None,
        "realized_pnl": result.realized_pnl_total,
        "income_expense": getattr(result, "income_expense_total", ZERO),
        "win_rate": result.win_rate,
        "closed_episodes": len(result.closed_episodes),
        "max_drawdown": max_drawdown if has_performance else None,
        "sharpe_ratio": sharpe if has_performance else None,
    }


def _daily_series(
    result: ReplayResult,
    quotes: dict[str, dict[str, dict[str, Any]]],
    days: list[str],
    sessions: list[str],
) -> list[dict[str, Any]]:
    if not sessions or not days:
        return []

    session_set = set(sessions)
    events_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in result.effective_events:
        session = _session_for_event(event, sessions)
        if session is not None:
            events_by_session[session].append(event)

    cash = ZERO
    positions: dict[str, Decimal] = defaultdict(lambda: ZERO)
    position_meta: dict[str, dict[str, Any]] = {}
    cumulative_external_flow = ZERO
    previous_nav: Decimal | None = None
    previous_segment_return: Decimal | None = None
    segment_id = 0
    gap_seen = False
    daily: list[dict[str, Any]] = []

    open_event = next(
        event
        for event in result.effective_events
        if event["action"] == "PORTFOLIO_OPEN"
    )
    open_session = _session_for_event(open_event, sessions)
    if open_session is None:
        return []

    for session in (day for day in days if day >= open_session):
        external_flow = ZERO
        for event in events_by_session.get(session, []):
            action = event["action"]
            if action == "PORTFOLIO_OPEN":
                cash = money(event["initial_cash"], field="initial_cash")
            elif action == "CASH_FLOW":
                flow = money(event["amount"])
                cash = money(cash + flow)
                external_flow = money(external_flow + flow)
                cumulative_external_flow = money(cumulative_external_flow + flow)
            elif action == "INCOME_EXPENSE":
                cash = money(cash + money(event["amount"]))
            elif action == "BUY":
                quantity = shares(event["shares"])
                unit_price = price(event["price"])
                fee = money(event.get("fee", 0), field="fee")
                settlement_adjustment = money(
                    event.get("settlement_adjustment", 0),
                    field="settlement_adjustment",
                )
                instrument_id = event.get("instrument_id") or event["symbol"]
                multiplier = shares(
                    event.get("contract_multiplier", "1"),
                    field="contract_multiplier",
                )
                position_meta[instrument_id] = {
                    "symbol": event["symbol"],
                    "quote_key": _position_quote_key(event),
                    "contract_multiplier": multiplier,
                }
                positions[instrument_id] = shares(
                    positions[instrument_id] + quantity
                )
                cash = money(
                    cash
                    - amount_for(quantity * multiplier, unit_price)
                    - fee
                    + settlement_adjustment
                )
            elif action == "SELL":
                quantity = shares(event["shares"])
                unit_price = price(event["price"])
                fee = money(event.get("fee", 0), field="fee")
                settlement_adjustment = money(
                    event.get("settlement_adjustment", 0),
                    field="settlement_adjustment",
                )
                instrument_id = event.get("instrument_id") or event["symbol"]
                multiplier = shares(
                    event.get("contract_multiplier", "1"),
                    field="contract_multiplier",
                )
                position_meta.setdefault(
                    instrument_id,
                    {
                        "symbol": event["symbol"],
                        "quote_key": _position_quote_key(event),
                        "contract_multiplier": multiplier,
                    },
                )
                positions[instrument_id] = shares(
                    positions[instrument_id] - quantity
                )
                cash = money(
                    cash
                    + amount_for(quantity * multiplier, unit_price)
                    - fee
                    + settlement_adjustment
                )
            elif action == "SPLIT":
                instrument_id = event["instrument_id"]
                ratio = (
                    shares(event["numerator"], field="numerator")
                    / shares(event["denominator"], field="denominator")
                )
                positions[instrument_id] = shares(
                    positions[instrument_id] * ratio
                )

        missing_symbols = sorted(
            {
                position_meta[instrument_id]["symbol"]
                for instrument_id, quantity in positions.items()
                if quantity > 0
                and _quote_for_day(
                    quotes.get(
                        position_meta[instrument_id]["quote_key"],
                        {},
                    ),
                    session,
                    session_set,
                )
                is None
            }
        )
        if missing_symbols:
            daily.append(
                {
                    "date": session,
                    "nav": None,
                    "cash": cash,
                    "external_flow": external_flow,
                    "daily_return": None,
                    "cumulative_return": None,
                    "segment_id": None,
                    "segment_return": None,
                    "pnl": None,
                    "data_status": "INSUFFICIENT_MARKET_DATA",
                    "missing_symbols": missing_symbols,
                }
            )
            previous_nav = None
            previous_segment_return = None
            gap_seen = True
            continue

        market_value = money(
            sum(
                (
                    amount_for(
                        quantity
                        * position_meta[instrument_id]["contract_multiplier"],
                        _quote_for_day(
                            quotes[position_meta[instrument_id]["quote_key"]],
                            session,
                            session_set,
                        )["close"],
                    )
                    for instrument_id, quantity in positions.items()
                    if quantity > 0
                ),
                ZERO,
            )
        )
        nav = money(cash + market_value)

        pnl = money(nav - result.initial_cash - cumulative_external_flow)
        if previous_nav is None:
            segment_id += 1
            daily_return = None if gap_seen else ZERO
            segment_return = ZERO
        else:
            denominator = money(previous_nav + external_flow)
            if denominator <= 0:
                daily.append(
                    {
                        "date": session,
                        "nav": nav,
                        "cash": cash,
                        "external_flow": external_flow,
                        "daily_return": None,
                        "cumulative_return": None,
                        "segment_id": None,
                        "segment_return": None,
                        "pnl": pnl,
                        "data_status": "INSUFFICIENT_DATA",
                        "missing_symbols": [],
                    }
                )
                previous_nav = None
                previous_segment_return = None
                gap_seen = True
                continue
            daily_return = percent(nav / denominator - 1)
            base = previous_segment_return if previous_segment_return is not None else ZERO
            segment_return = percent((1 + base) * (1 + daily_return) - 1)

        cumulative_return = None if gap_seen else segment_return
        daily.append(
            {
                "date": session,
                "nav": nav,
                "cash": cash,
                "external_flow": external_flow,
                "daily_return": daily_return,
                "cumulative_return": cumulative_return,
                "segment_id": segment_id,
                "segment_return": segment_return,
                "pnl": pnl,
                "data_status": "OK",
                "missing_symbols": [],
            }
        )
        previous_nav = nav
        previous_segment_return = segment_return

    return daily


def _benchmark_series(
    benchmark: dict[str, dict[str, Any]],
    days: list[str],
    sessions: list[str],
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    session_set = set(sessions)
    previous: Decimal | None = None
    segment_return: Decimal | None = None
    segment_id = 0
    gap_seen = False
    for session in days:
        record = _quote_for_day(benchmark, session, session_set)
        if record is None:
            series.append(
                {
                    "date": session,
                    "close": None,
                    "daily_return": None,
                    "cumulative_return": None,
                    "segment_id": None,
                    "segment_return": None,
                    "data_status": "INSUFFICIENT_MARKET_DATA",
                }
            )
            previous = None
            segment_return = None
            gap_seen = True
            continue
        close = record["close"]
        if previous is None:
            segment_id += 1
            daily_return = None
            segment_return = ZERO
        else:
            daily_return = percent(close / previous - 1)
            segment_return = percent(
                (1 + (segment_return if segment_return is not None else ZERO))
                * (1 + daily_return)
                - 1
            )
        series.append(
            {
                "date": session,
                "close": close,
                "daily_return": daily_return,
                "cumulative_return": None if gap_seen else segment_return,
                "segment_id": segment_id,
                "segment_return": segment_return,
                "data_status": "OK",
            }
        )
        previous = close
    return series


def _portfolio_calendar(
    result: ReplayResult,
    market_sessions: list[str],
) -> tuple[list[str], list[str]]:
    """Build a portfolio-local calendar.

    Market sessions are shared source data, but a future-dated Paper event must
    not extend Live (or the benchmark) into a session for which no quote exists.
    Each portfolio may extend only its own terminal session.
    """

    candidates = set(market_sessions)
    candidates.update(
        _event_session_candidate(event)
        for event in result.effective_events
    )
    if not candidates:
        return [], []
    sessions = nyse_sessions(
        date.fromisoformat(min(candidates)),
        date.fromisoformat(max(candidates)),
    )
    days = _calendar_days(
        date.fromisoformat(sessions[0]),
        date.fromisoformat(sessions[-1]),
    )
    return sessions, days


def _build_snapshot_locked(
    root_path: Path,
    store: LedgerStore,
    *,
    output: str | Path | None = None,
    write: bool = True,
    repair_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    repairs = repair_records if repair_records is not None else []
    paper_events = store.read(
        "paper",
        repair_tail=True,
        repair_records=repairs,
    )
    live_events = store.read(
        "live",
        repair_tail=True,
        repair_records=repairs,
    )
    market_events = store.read(
        "market",
        repair_tail=True,
        repair_records=repairs,
    )
    _assert_pending_batch_complete(
        root_path,
        paper_events + live_events + market_events,
    )
    quotes, benchmark, market_sessions = _market_data(market_events)
    replay_results = {
        name: replay_portfolio(events, portfolio=name)
        for name, events in (("paper", paper_events), ("live", live_events))
        if events
    }
    benchmark_days = (
        _calendar_days(
            date.fromisoformat(market_sessions[0]),
            date.fromisoformat(market_sessions[-1]),
        )
        if market_sessions
        else []
    )
    warnings = [
        (
            f"{Path(repair['ledger']).stem} ledger tail repaired; "
            f"{repair['bytes_quarantined']} bytes quarantined"
        )
        for repair in repairs
    ]
    portfolios: dict[str, Any] = {}

    for name, events in (("paper", paper_events), ("live", live_events)):
        if not events:
            warnings.append(f"{name} portfolio has no events")
            portfolios[name] = {
                "data_status": "NO_DATA",
                "holdings": [],
                "recent_trades": [],
                "daily": [],
                "metrics": {
                    "data_status": "NO_DATA",
                    "performance_effective_date": None,
                    "performance_scope": None,
                    "total_return": None,
                    "realized_pnl": "0",
                    "income_expense": "0",
                    "win_rate": None,
                    "closed_episodes": 0,
                    "max_drawdown": None,
                    "sharpe_ratio": None,
                },
            }
            continue

        result = replay_results[name]
        portfolio_sessions, portfolio_days = _portfolio_calendar(
            result,
            market_sessions,
        )
        daily = _daily_series(
            result,
            quotes,
            portfolio_days,
            portfolio_sessions,
        )
        last_day = portfolio_days[-1] if portfolio_days else None
        holdings = _enrich_holdings(
            result,
            quotes,
            last_day,
            set(portfolio_sessions),
        )
        metrics = _metrics(daily, result)
        if metrics["data_status"] != "OK":
            warnings.append(f"{name} performance contains incomplete market data")
        elif metrics["performance_scope"] == "LATEST_COMPLETE_SEGMENT":
            warnings.append(
                f"{name} performance starts at "
                f"{metrics['performance_effective_date']} after incomplete "
                "market data"
            )
        portfolios[name] = {
            "data_status": metrics["data_status"],
            "cash": result.cash,
            "initial_cash": result.initial_cash,
            "holdings": holdings,
            "recent_trades": list(reversed(result.trade_history)),
            "realized_pnl_per_trade": result.realized_pnl_per_trade,
            "daily": daily,
            "metrics": metrics,
        }

    source_head = {
        "paper": _source_head(store.path_for("paper"), paper_events),
        "live": _source_head(store.path_for("live"), live_events),
        "market": _source_head(store.path_for("market"), market_events),
    }
    all_events = paper_events + live_events + market_events
    latest_event = max(
        all_events,
        key=lambda event: parse_timestamp(
            event["occurred_at"],
            field="occurred_at",
        ),
        default=None,
    )
    latest_market_event = max(
        market_events,
        key=lambda event: parse_timestamp(
            event["occurred_at"],
            field="occurred_at",
        ),
        default=None,
    )
    latest_event_time = latest_event["occurred_at"] if latest_event else None
    prices_as_of = (
        latest_market_event["occurred_at"] if latest_market_event else None
    )
    recorded_market_sessions = sorted(
        {
            event["session_date"]
            for event in market_events
            if event["action"] in {"QUOTE", "BENCHMARK_CLOSE"}
        }
    )
    if recorded_market_sessions:
        latest_completed = _latest_completed_session(datetime.now(UTC))
        expected_sessions = nyse_sessions(
            date.fromisoformat(recorded_market_sessions[-1])
            + timedelta(days=1),
            latest_completed,
        )
        if len(expected_sessions) > 1:
            warnings.append("prices > 1 trading day stale")
    revision = sum(head["count"] for head in source_head.values())
    snapshot = json_safe(
        {
            "schema_version": 4,
            "revision": revision,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "data_as_of": latest_event_time,
            "prices_as_of": prices_as_of,
            "currency": "USD",
            "source_head": source_head,
            "portfolios": portfolios,
            "benchmark": {
                "symbol": "SPY",
                "daily": _benchmark_series(
                    benchmark,
                    benchmark_days,
                    market_sessions,
                ),
            },
            "warnings": warnings,
        }
    )
    validate_snapshot(snapshot)

    if write:
        target = Path(output) if output else root_path / "snapshots" / "portfolio-snapshot.json"
        atomic_write_json(target, snapshot, mode=0o600)
    return snapshot


def build_snapshot(
    root: str | Path,
    *,
    output: str | Path | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build one consistent public snapshot under the global ledger lock.

    Clearing rebuild.pending while the same lock is held prevents a concurrent
    append from having its newer recovery marker accidentally removed.
    """

    root_path = Path(root)
    store = LedgerStore(root_path)
    with FileLock(store.lock_path):
        snapshot = _build_snapshot_locked(
            root_path,
            store,
            output=output,
            write=write,
        )
        if write:
            durable_unlink(root_path / "state" / "rebuild.pending")
        return snapshot


def build_snapshot_if_needed(
    root: str | Path,
    *,
    output: str | Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Rebuild only when a ledger source head differs from the last snapshot."""

    root_path = Path(root)
    store = LedgerStore(root_path)
    target = Path(output) if output else root_path / "snapshots" / "portfolio-snapshot.json"
    with FileLock(store.lock_path):
        repairs: list[dict[str, Any]] = []
        paper_events = store.read(
            "paper",
            repair_tail=True,
            repair_records=repairs,
        )
        live_events = store.read(
            "live",
            repair_tail=True,
            repair_records=repairs,
        )
        market_events = store.read(
            "market",
            repair_tail=True,
            repair_records=repairs,
        )
        current_heads = {
            "paper": _source_head(store.path_for("paper"), paper_events),
            "live": _source_head(store.path_for("live"), live_events),
            "market": _source_head(store.path_for("market"), market_events),
        }
        _assert_pending_batch_complete(
            root_path,
            paper_events + live_events + market_events,
        )
        current: dict[str, Any] | None = None
        if target.exists():
            try:
                parsed = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    current = parsed
            except (OSError, json.JSONDecodeError):
                current = None
        if current is not None:
            try:
                validate_snapshot(current)
            except ValidationError:
                current = None
        structurally_current = (
            current is not None
            and current.get("source_head") == current_heads
        )
        if structurally_current and not repairs:
            durable_unlink(root_path / "state" / "rebuild.pending")
            return current, False

        snapshot = _build_snapshot_locked(
            root_path,
            store,
            output=target,
            write=True,
            repair_records=repairs,
        )
        durable_unlink(root_path / "state" / "rebuild.pending")
        return snapshot, True
