#!/usr/local/bin/python3
"""Fetch completed US closes and submit one idempotent market quote batch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone as dt_timezone
from pathlib import Path


def _add_backend_to_path() -> None:
    candidates = (
        Path("/data/portfolio-tracker/backend"),
        Path(__file__).resolve().parents[1] / "backend",
    )
    for candidate in candidates:
        if candidate.is_dir():
            selected = str(candidate)
            if selected not in sys.path:
                sys.path.insert(0, selected)
            return
    raise RuntimeError("portfolio tracker backend is unavailable")


_add_backend_to_path()

from portfolio_tracker.market_time import (  # noqa: E402
    is_nyse_session,
    new_york_close_utc,
)


# Suppress yfinance debug noise before importing it.
os.environ["YF_QUIET"] = "1"
import yfinance as yf  # noqa: E402


SYMBOLS = [
    # Holdings (Live + Paper watchlist)
    "MU",
    "NVDA",
    "VOO",
    "SPCX",
    "SKHY",
    # Paper swing trade watchlist
    "AAPL",
    "MSFT",
    "AMZN",
    "META",
    "GOOGL",
    "AVGO",
    "AMD",
    "MRVL",
    "AMAT",
    "LRCX",
    # Other live portfolio symbols
    "TSLA",
    "ABBV",
    "BRK.B",
    "INDA",
    "INTC",
    "KO",
    "MCD",
    "ONDS",
    "PLTR",
    "QQQ",
    "SARK",
    "SGOV",
    "SNDK",
    "SQQQ",
    "TQQQ",
    # SPY always emits both a raw holding quote and an adjusted benchmark.
    "SPY",
]
BENCHMARK_SYMBOL = "SPY"

# Map portfolio ticker to yfinance ticker (for example BRK.B to BRK-B).
SYMBOL_MAP = {"BRK.B": "BRK-B"}

BRIDGE_CMD = [
    "/usr/local/bin/python3",
    "/data/portfolio-tracker/backend/integrations/hermes_bridge.py",
    "--root",
    "/data/portfolio",
]

UTC = dt_timezone.utc
HKT = dt_timezone(timedelta(hours=8))


def now_utc() -> datetime:
    return datetime.now(UTC)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def session_occurred_at(session_date: str) -> str:
    """Return the deterministic official close timestamp for one session."""

    try:
        session = date.fromisoformat(session_date)
    except ValueError as exc:
        raise RuntimeError("provider returned an invalid session date") from exc
    if not is_nyse_session(session):
        raise RuntimeError("provider returned a non-NYSE session")
    return _iso_utc(new_york_close_utc(session))


def find_last_trading_day() -> tuple[str, str]:
    """Return the latest SPY session and its deterministic official close."""

    ticker = yf.Ticker(BENCHMARK_SYMBOL)
    df = ticker.history(period="7d", interval="1d")
    if df.empty:
        raise RuntimeError("Cannot fetch SPY data to determine trading day")

    df = df[df["Volume"] > 0]
    if df.empty:
        raise RuntimeError("No SPY trading data in last 7 days")

    session_date = df.index[-1].strftime("%Y-%m-%d")
    return session_date, session_occurred_at(session_date)


def fetch_close(
    symbol: str,
    session_date: str,
    *,
    adjust: bool = True,
) -> float | None:
    """Fetch adjusted benchmark close or raw holding close for one session."""

    try:
        ticker = yf.Ticker(symbol)
        end_date = (
            date.fromisoformat(session_date) + timedelta(days=1)
        ).isoformat()
        df = ticker.history(
            start=session_date,
            end=end_date,
            interval="1d",
            auto_adjust=adjust,
        )

        if df.empty:
            df = ticker.history(
                period="5d",
                interval="1d",
                auto_adjust=adjust,
            )
            df = df[df.index.strftime("%Y-%m-%d") == session_date]

        if df.empty or df["Volume"].sum() == 0:
            return None

        return float(df["Close"].iloc[-1])
    except Exception as exc:
        print(f"  warning: {symbol}: {exc}", file=sys.stderr)
        return None


def _event_id(session_date: str, symbol: str, *, benchmark: bool) -> str:
    if benchmark:
        return f"market-benchmark-{session_date}-{symbol.lower()}"
    return f"market-quote-{session_date}-{symbol.lower()}"


def _quote_event(
    *,
    session_date: str,
    occurred_at: str,
    symbol: str,
    close: float,
    benchmark: bool,
) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": _event_id(
            session_date,
            symbol,
            benchmark=benchmark,
        ),
        "occurred_at": occurred_at,
        # A retry must reproduce the complete payload, not only the event ID.
        "created_at": occurred_at,
        "session_date": session_date,
        "symbol": symbol,
        "close": str(round(close, 4)),
    }
    if benchmark:
        event["benchmark"] = True
    return event


def build_quote_batch(session_date: str, occurred_at: str) -> list[dict]:
    """Fetch all prices and build a byte-for-byte retryable quote batch."""

    expected_occurred_at = session_occurred_at(session_date)
    if occurred_at != expected_occurred_at:
        raise RuntimeError(
            "occurred_at must equal the official session close "
            f"({expected_occurred_at})"
        )

    events: list[dict] = []
    benchmark_added = False

    for symbol in SYMBOLS:
        yf_symbol = SYMBOL_MAP.get(symbol, symbol)
        raw_close = fetch_close(yf_symbol, session_date, adjust=False)
        if raw_close is None:
            print(
                f"  error: {symbol}: no raw data for {session_date}",
                file=sys.stderr,
            )
        else:
            events.append(
                _quote_event(
                    session_date=session_date,
                    occurred_at=occurred_at,
                    symbol=symbol,
                    close=raw_close,
                    benchmark=False,
                )
            )
            print(f"  quote {symbol}: ${raw_close:.2f}")

        if symbol != BENCHMARK_SYMBOL:
            continue

        adjusted_close = fetch_close(
            yf_symbol,
            session_date,
            adjust=True,
        )
        if adjusted_close is None:
            raise RuntimeError(
                f"{BENCHMARK_SYMBOL}: no adjusted benchmark data for "
                f"{session_date}"
            )
        events.append(
            _quote_event(
                session_date=session_date,
                occurred_at=occurred_at,
                symbol=symbol,
                close=adjusted_close,
                benchmark=True,
            )
        )
        benchmark_added = True
        print(f"  benchmark {symbol}: ${adjusted_close:.2f}")

    if not benchmark_added:
        raise RuntimeError("quote batch has no SPY benchmark")
    return events


def submit_quote_batch(events: list[dict]) -> bool:
    """Submit one quote batch to hermes_bridge through standard input."""

    if not events:
        print("No events to submit.", file=sys.stderr)
        return False

    payload = json.dumps(events, ensure_ascii=False)
    cmd = BRIDGE_CMD + ["quote-batch", "--file", "-"]

    try:
        result = subprocess.run(
            cmd,
            input=payload,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            print(
                f"Bridge error: {result.stderr.strip()[-500:]}",
                file=sys.stderr,
            )
            return False

        response = json.loads(result.stdout)
        status = response.get("status", "unknown")
        appended = response.get("appended", 0)
        duplicates = response.get("duplicates", 0)
        print(
            f"Submitted: {appended} new, {duplicates} duplicates -> {status}"
        )
        return True
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"Bridge failed: {exc}", file=sys.stderr)
        return False


def main() -> None:
    now_hkt = now_utc().astimezone(HKT)
    print(f"Market Quotes - {now_hkt.strftime('%Y-%m-%d %H:%M HKT')}")

    try:
        session_date, occurred_at = find_last_trading_day()
        print(f"Session: {session_date} ({occurred_at})")
        print(f"Fetching closes for {len(SYMBOLS)} symbols...")
        events = build_quote_batch(session_date, occurred_at)
    except RuntimeError as exc:
        print(f"Cannot build quote batch: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not events:
        print("No price data fetched.", file=sys.stderr)
        raise SystemExit(1)

    print(f"Submitting {len(events)} quote events...")
    if not submit_quote_batch(events):
        print("Quote submission failed.", file=sys.stderr)
        raise SystemExit(1)
    print("Market quotes submitted successfully.")


if __name__ == "__main__":
    main()
