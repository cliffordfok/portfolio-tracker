"""Create deterministic placeholder data for the first GitHub Pages render."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from portfolio_tracker.ledger import LedgerStore, atomic_write_json
from portfolio_tracker.snapshot import build_snapshot


SESSIONS = [
    ("2026-06-01", {"AAPL": "195", "NVDA": "141", "MSFT": "488", "SPY": "596"}),
    ("2026-06-05", {"AAPL": "198", "NVDA": "142", "MSFT": "491", "SPY": "599"}),
    ("2026-06-10", {"AAPL": "201", "NVDA": "145", "MSFT": "493", "SPY": "597"}),
    ("2026-06-15", {"AAPL": "205", "NVDA": "144", "MSFT": "489", "SPY": "601"}),
    ("2026-06-19", {"AAPL": "203", "NVDA": "147", "MSFT": "492", "SPY": "603"}),
    ("2026-06-25", {"AAPL": "207", "NVDA": "149", "MSFT": "490", "SPY": "606"}),
    ("2026-07-01", {"AAPL": "209", "NVDA": "151", "MSFT": "495", "SPY": "609"}),
    ("2026-07-06", {"AAPL": "208", "NVDA": "148", "MSFT": "493", "SPY": "607"}),
    ("2026-07-10", {"AAPL": "211", "NVDA": "150", "MSFT": "497", "SPY": "612"}),
    ("2026-07-17", {"AAPL": "214", "NVDA": "153", "MSFT": "501", "SPY": "616"}),
]


def make_event(
    *,
    event_id: str,
    portfolio: str,
    action: str,
    occurred_at: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "portfolio": portfolio,
        "occurred_at": occurred_at,
        "created_at": occurred_at,
        "source": "demo-seed",
        "action": action,
        **fields,
    }


def portfolio_events() -> list[dict[str, Any]]:
    return [
        make_event(
            event_id="paper-open-demo",
            portfolio="paper",
            action="PORTFOLIO_OPEN",
            occurred_at="2026-05-31T14:00:00Z",
            initial_cash="100000",
        ),
        make_event(
            event_id="paper-buy-aapl-001",
            portfolio="paper",
            action="BUY",
            occurred_at="2026-06-01T14:35:00Z",
            symbol="AAPL",
            shares="100",
            price="195",
            fee="0",
            note="突破後分批建立倉位",
            reason="價格站上區間高位",
            strategy="momentum",
        ),
        make_event(
            event_id="paper-buy-nvda-001",
            portfolio="paper",
            action="BUY",
            occurred_at="2026-06-05T15:10:00Z",
            symbol="NVDA",
            shares="60",
            price="142",
            fee="0",
            note="AI 產業動能延續",
            reason="相對強度改善",
            strategy="momentum",
        ),
        make_event(
            event_id="paper-sell-aapl-001",
            portfolio="paper",
            action="SELL",
            occurred_at="2026-06-15T17:20:00Z",
            symbol="AAPL",
            shares="40",
            price="205",
            fee="0",
            note="分段鎖定利潤",
            reason="到達第一目標價",
            strategy="momentum",
        ),
        make_event(
            event_id="paper-buy-msft-001",
            portfolio="paper",
            action="BUY",
            occurred_at="2026-06-25T15:00:00Z",
            symbol="MSFT",
            shares="30",
            price="490",
            fee="0",
            note="雲端業務趨勢穩定",
            reason="回踩支持後轉強",
            strategy="swing",
        ),
        make_event(
            event_id="paper-sell-nvda-001",
            portfolio="paper",
            action="SELL",
            occurred_at="2026-07-10T16:00:00Z",
            symbol="NVDA",
            shares="20",
            price="150",
            fee="0",
            note="降低單一產業曝險",
            reason="倉位再平衡",
            strategy="momentum",
        ),
        make_event(
            event_id="live-open-demo",
            portfolio="live",
            action="PORTFOLIO_OPEN",
            occurred_at="2026-05-31T14:00:00Z",
            initial_cash="50000",
        ),
        make_event(
            event_id="live-cash-001",
            portfolio="live",
            action="CASH_FLOW",
            occurred_at="2026-06-01T13:00:00Z",
            amount="10000",
            note="示範入金",
        ),
        make_event(
            event_id="live-buy-aapl-001",
            portfolio="live",
            action="BUY",
            occurred_at="2026-06-01T15:00:00Z",
            symbol="AAPL",
            shares="30",
            price="196",
            fee="1",
            note="示範真實交易",
        ),
        make_event(
            event_id="live-buy-spy-001",
            portfolio="live",
            action="BUY",
            occurred_at="2026-06-10T15:00:00Z",
            symbol="SPY",
            shares="20",
            price="597",
            fee="1",
            note="核心指數倉位",
        ),
        make_event(
            event_id="live-sell-aapl-001",
            portfolio="live",
            action="SELL",
            occurred_at="2026-07-06T14:40:00Z",
            symbol="AAPL",
            shares="10",
            price="208",
            fee="1",
            note="部分止盈",
        ),
        make_event(
            event_id="live-buy-msft-001",
            portfolio="live",
            action="BUY",
            occurred_at="2026-07-10T15:30:00Z",
            symbol="MSFT",
            shares="10",
            price="493",
            fee="1",
            note="建立雲端軟件倉位",
        ),
    ]


def market_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for session, closes in SESSIONS:
        close_time = f"{session}T20:30:00Z"
        events.append(
            make_event(
                event_id=f"market-spy-benchmark-{session}",
                portfolio="market",
                action="BENCHMARK_CLOSE",
                occurred_at=close_time,
                symbol="SPY",
                close=closes["SPY"],
                session_date=session,
            )
        )
        for symbol, close in closes.items():
            events.append(
                make_event(
                    event_id=f"market-{symbol.lower()}-{session}",
                    portfolio="market",
                    action="QUOTE",
                    occurred_at=close_time,
                    symbol=symbol,
                    close=close,
                    session_date=session,
                )
            )
    return events


def public_trade(
    event: dict[str, Any],
    current_prices: dict[str, str],
    derived: dict[str, Any] | None,
) -> dict[str, Any]:
    occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
    return {
        "date": occurred.astimezone(UTC).date().isoformat(),
        "symbol": event["symbol"],
        "action": event["action"],
        "shares": float(event["shares"]),
        "price": float(event["price"]),
        "fee": float(event.get("fee", 0)),
        "note": event.get("note", ""),
        "reason": event.get("reason", ""),
        "strategy": event.get("strategy", ""),
        "current_price": float(current_prices[event["symbol"]]),
        "pnl": float(derived["pnl"]) if derived and derived.get("pnl") is not None else None,
        "pnl_pct": (
            float(derived["pnl_pct"])
            if derived and derived.get("pnl_pct") is not None
            else None
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    runtime = Path(args.runtime).resolve()
    output = Path(args.output).resolve()
    if any((runtime / "ledger").glob("*.jsonl")):
        raise SystemExit("runtime already contains ledgers; choose an empty directory")

    store = LedgerStore(runtime)
    economic = portfolio_events()
    for item in economic + market_events():
        store.append(item)

    output.mkdir(parents=True, exist_ok=True)
    snapshot = build_snapshot(
        runtime,
        output=output / "portfolio-snapshot.json",
        write=True,
    )
    latest = SESSIONS[-1][1]
    derived_by_portfolio = {
        name: {
            trade["event_id"]: trade
            for trade in snapshot["portfolios"][name]["recent_trades"]
        }
        for name in ("paper", "live")
    }
    paper = [
        public_trade(item, latest, derived_by_portfolio["paper"].get(item["event_id"]))
        for item in economic
        if item["portfolio"] == "paper" and item["action"] in {"BUY", "SELL"}
    ]
    live = [
        public_trade(item, latest, derived_by_portfolio["live"].get(item["event_id"]))
        for item in economic
        if item["portfolio"] == "live" and item["action"] in {"BUY", "SELL"}
    ]
    benchmark = [
        {"date": session, "close": float(closes["SPY"])}
        for session, closes in SESSIONS
    ]
    atomic_write_json(output / "paper.json", paper, mode=0o644)
    atomic_write_json(output / "live.json", live, mode=0o644)
    atomic_write_json(output / "benchmark.json", benchmark, mode=0o644)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
