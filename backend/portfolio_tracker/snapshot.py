"""Build the public, derived dashboard snapshot from private master ledgers."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .decimal_utils import ZERO, amount_for, json_safe, money, percent, price, shares
from .errors import BusinessInvariantError
from .ledger import FileLock, LedgerStore, atomic_write_json, durable_unlink
from .replay import ReplayResult, replay_portfolio
from .schemas import parse_timestamp

try:
    NEW_YORK: ZoneInfo | None = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    NEW_YORK = None
MARKET_CLOSE = time(16, 0)


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
        }
        if event["action"] == "QUOTE":
            quotes[event["symbol"]][session] = record
        elif event["action"] == "BENCHMARK_CLOSE":
            benchmark[session] = record
            quotes[event["symbol"]][session] = record
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


def _quote_for_day(
    records: dict[str, dict[str, Any]],
    day: str,
    trading_sessions: set[str],
) -> dict[str, Any] | None:
    if day in trading_sessions:
        return records.get(day)
    eligible = [session for session in records if session < day]
    return records[max(eligible)] if eligible else None


def _session_for_event(event: dict[str, Any], sessions: list[str]) -> str | None:
    if not sessions:
        return None
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
    session_text = session_day.isoformat()
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


def _enrich_holdings(
    result: ReplayResult,
    quotes: dict[str, dict[str, dict[str, Any]]],
    last_day: str | None,
    trading_sessions: set[str],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for holding in result.holdings:
        symbol = holding["symbol"]
        current_quote = (
            _quote_for_day(quotes.get(symbol, {}), last_day, trading_sessions)
            if last_day
            else None
        )
        current_price = current_quote["close"] if current_quote else None
        market_value = (
            amount_for(holding["shares"], current_price)
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
    has_gap = not daily or any(item["data_status"] != "OK" for item in daily)
    valid_returns = [
        item["daily_return"]
        for item in daily
        if item["daily_return"] is not None
    ]
    total_return = daily[-1]["cumulative_return"] if daily else None
    max_drawdown: Decimal | None = None
    sharpe: Decimal | None = None

    if not has_gap and daily:
        peak = Decimal("-1")
        drawdowns: list[Decimal] = []
        for item in daily:
            cumulative = item["cumulative_return"]
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

    return {
        "data_status": "INSUFFICIENT_DATA" if has_gap else "OK",
        "total_return": None if has_gap else total_return,
        "realized_pnl": result.realized_pnl_total,
        "win_rate": result.win_rate,
        "closed_episodes": len(result.closed_episodes),
        "max_drawdown": None if has_gap else max_drawdown,
        "sharpe_ratio": None if has_gap else sharpe,
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
            elif action == "BUY":
                quantity = shares(event["shares"])
                unit_price = price(event["price"])
                fee = money(event.get("fee", 0), field="fee")
                positions[event["symbol"]] = shares(
                    positions[event["symbol"]] + quantity
                )
                cash = money(cash - amount_for(quantity, unit_price) - fee)
            elif action == "SELL":
                quantity = shares(event["shares"])
                unit_price = price(event["price"])
                fee = money(event.get("fee", 0), field="fee")
                positions[event["symbol"]] = shares(
                    positions[event["symbol"]] - quantity
                )
                cash = money(cash + amount_for(quantity, unit_price) - fee)

        missing_symbols = sorted(
            symbol
            for symbol, quantity in positions.items()
            if quantity > 0
            and _quote_for_day(quotes.get(symbol, {}), session, session_set) is None
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
                        quantity,
                        _quote_for_day(
                            quotes[symbol], session, session_set
                        )["close"],
                    )
                    for symbol, quantity in positions.items()
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
    quotes, benchmark, market_sessions = _market_data(market_events)
    replay_results = {
        name: replay_portfolio(events, portfolio=name)
        for name, events in (("paper", paper_events), ("live", live_events))
        if events
    }
    session_candidates = set(market_sessions)
    for result in replay_results.values():
        for event in result.effective_events:
            occurred = parse_timestamp(event["occurred_at"], field="occurred_at")
            local = _new_york_local(occurred)
            candidate = local.date()
            if (
                local.timetz().replace(tzinfo=None) > MARKET_CLOSE
                or not is_nyse_session(candidate)
            ):
                candidate += timedelta(days=1)
                while not is_nyse_session(candidate):
                    candidate += timedelta(days=1)
            session_candidates.add(candidate.isoformat())
    sessions = (
        nyse_sessions(
            date.fromisoformat(min(session_candidates)),
            date.fromisoformat(max(session_candidates)),
        )
        if session_candidates
        else []
    )
    days = (
        _calendar_days(
            date.fromisoformat(sessions[0]),
            date.fromisoformat(sessions[-1]),
        )
        if sessions
        else []
    )
    session_set = set(sessions)
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
                    "total_return": None,
                    "realized_pnl": "0",
                    "win_rate": None,
                    "closed_episodes": 0,
                    "max_drawdown": None,
                    "sharpe_ratio": None,
                },
            }
            continue

        result = replay_results[name]
        daily = _daily_series(result, quotes, days, sessions)
        last_day = days[-1] if days else None
        holdings = _enrich_holdings(result, quotes, last_day, session_set)
        metrics = _metrics(daily, result)
        if metrics["data_status"] != "OK":
            warnings.append(f"{name} performance contains incomplete market data")
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
            "schema_version": 3,
            "revision": revision,
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "data_as_of": latest_event_time,
            "prices_as_of": prices_as_of,
            "currency": "USD",
            "source_head": source_head,
            "portfolios": portfolios,
            "benchmark": {
                "symbol": "SPY",
                "daily": _benchmark_series(benchmark, days, sessions),
            },
            "warnings": warnings,
        }
    )

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
        current: dict[str, Any] | None = None
        if target.exists():
            try:
                parsed = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    current = parsed
            except (OSError, json.JSONDecodeError):
                current = None
        structurally_current = (
            current is not None
            and current.get("schema_version") == 3
            and isinstance(current.get("portfolios"), dict)
            and isinstance(current.get("benchmark"), dict)
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
