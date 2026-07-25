"""Build the public, derived dashboard snapshot from private master ledgers."""

from __future__ import annotations

import hashlib
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import stdev
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
) -> tuple[dict[str, dict[str, Decimal]], dict[str, Decimal], list[str]]:
    quotes: dict[str, dict[str, Decimal]] = defaultdict(dict)
    benchmark: dict[str, Decimal] = {}
    for event in market_events:
        session = event["session_date"]
        close = price(event["close"], field="close")
        if event["action"] == "QUOTE":
            quotes[event["symbol"]][session] = close
        elif event["action"] == "BENCHMARK_CLOSE":
            benchmark[session] = close
            quotes[event["symbol"]][session] = close
    sessions = sorted(benchmark or {day for values in quotes.values() for day in values})
    return dict(quotes), benchmark, sessions


def _session_for_event(event: dict[str, Any], sessions: list[str]) -> str | None:
    if not sessions:
        return None
    occurred = parse_timestamp(event["occurred_at"], field="occurred_at")
    local = _new_york_local(occurred)
    local_day = local.date().isoformat()

    if local.timetz().replace(tzinfo=None) > MARKET_CLOSE:
        index = bisect_right(sessions, local_day)
    else:
        index = bisect_left(sessions, local_day)
    if index >= len(sessions):
        return None
    return sessions[index]


def _enrich_holdings(
    result: ReplayResult,
    quotes: dict[str, dict[str, Decimal]],
    last_session: str | None,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for holding in result.holdings:
        symbol = holding["symbol"]
        current_price = quotes.get(symbol, {}).get(last_session or "")
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
        if len(valid_returns) >= 2:
            as_float = [float(value) for value in valid_returns]
            deviation = stdev(as_float)
            if deviation:
                annualized = (
                    Decimal(str(sum(as_float) / len(as_float) / deviation))
                    * Decimal(252).sqrt()
                )
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
    quotes: dict[str, dict[str, Decimal]],
    sessions: list[str],
) -> list[dict[str, Any]]:
    if not sessions:
        return []

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

    for session in sessions:
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
            if quantity > 0 and session not in quotes.get(symbol, {})
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
                    amount_for(quantity, quotes[symbol][session])
                    for symbol, quantity in positions.items()
                    if quantity > 0
                ),
                ZERO,
            )
        )
        nav = money(cash + market_value)

        if previous_nav is None:
            segment_id += 1
            daily_return = None
            segment_return = ZERO
        else:
            denominator = money(previous_nav + external_flow)
            if denominator == 0:
                segment_id += 1
                daily_return = None
                segment_return = ZERO
                gap_seen = True
            else:
                daily_return = percent(nav / denominator - 1)
                base = previous_segment_return if previous_segment_return is not None else ZERO
                segment_return = percent((1 + base) * (1 + daily_return) - 1)

        cumulative_return = None if gap_seen else segment_return
        pnl = money(nav - result.initial_cash - cumulative_external_flow)
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
    benchmark: dict[str, Decimal], sessions: list[str]
) -> list[dict[str, Any]]:
    series: list[dict[str, Any]] = []
    baseline: Decimal | None = None
    previous: Decimal | None = None
    for session in sessions:
        close = benchmark.get(session)
        if close is None:
            series.append(
                {
                    "date": session,
                    "close": None,
                    "daily_return": None,
                    "cumulative_return": None,
                    "data_status": "INSUFFICIENT_MARKET_DATA",
                }
            )
            previous = None
            continue
        if baseline is None:
            baseline = close
        series.append(
            {
                "date": session,
                "close": close,
                "daily_return": percent(close / previous - 1) if previous else None,
                "cumulative_return": percent(close / baseline - 1),
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
) -> dict[str, Any]:
    paper_events = store.read("paper")
    live_events = store.read("live")
    market_events = store.read("market")
    quotes, benchmark, sessions = _market_data(market_events)
    warnings: list[str] = []
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

        result = replay_portfolio(events, portfolio=name)
        daily = _daily_series(result, quotes, sessions)
        last_session = sessions[-1] if sessions else None
        holdings = _enrich_holdings(result, quotes, last_session)
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
    latest_event_time = max(
        (
            event["created_at"]
            for event in paper_events + live_events + market_events
        ),
        default=None,
    )
    prices_as_of = max(
        (event["occurred_at"] for event in market_events),
        default=None,
    )
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
                "daily": _benchmark_series(benchmark, sessions),
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
