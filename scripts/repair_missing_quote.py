#!/usr/bin/env python3
"""Safely append one verified, previously missing historical market quote."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
BRIDGE_PATH = BACKEND_ROOT / "integrations" / "hermes_bridge.py"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from portfolio_tracker.backup import backup_ledgers
from portfolio_tracker.decimal_utils import input_price
from portfolio_tracker.ledger import LedgerStore
from portfolio_tracker.market_time import new_york_close_utc, new_york_local
from portfolio_tracker.schemas import validate_event
from portfolio_tracker.market_time import is_nyse_session


class MissingQuoteError(ValueError):
    """Raised when a quote repair would be unsafe or ambiguous."""


@dataclass(frozen=True)
class QuotePlan:
    event: dict[str, Any]
    existing: dict[str, Any] | None
    benchmark_close: str | None


Runner = Callable[
    [Sequence[str]],
    subprocess.CompletedProcess[str],
]


def _occurred_at(session: date) -> str:
    return new_york_close_utc(session).isoformat().replace("+00:00", "Z")


def _quote_key(event: dict[str, Any]) -> str:
    return event.get("instrument_id") or event["symbol"]


def build_plan(
    root: Path,
    *,
    symbol: str,
    session_date: str,
    close: str,
    instrument_id: str | None = None,
    now: datetime | None = None,
    event_id: str | None = None,
) -> QuotePlan:
    root = root.resolve()
    if not root.is_dir():
        raise MissingQuoteError(f"runtime root not found: {root}")
    selected_symbol = symbol.strip().upper()
    try:
        session = date.fromisoformat(session_date)
    except ValueError as exc:
        raise MissingQuoteError(
            "session date must be a real YYYY-MM-DD date"
        ) from exc
    if session.isoformat() != session_date:
        raise MissingQuoteError("session date must use YYYY-MM-DD")
    if not is_nyse_session(session):
        raise MissingQuoteError(f"{session_date} is not an NYSE session")
    current = new_york_local(now or datetime.now(UTC)).date()
    if session >= current:
        raise MissingQuoteError(
            "refusing to repair the current or a future New York session"
        )
    normalized_close = format(input_price(close, field="close"), "f")
    selected_instrument_id = (
        instrument_id.strip().upper() if instrument_id else None
    )
    occurred_at = _occurred_at(session)
    event = {
        "event_id": event_id
        or f"market-manual-quote-{selected_symbol.lower()}-{session_date}",
        "portfolio": "market",
        "occurred_at": occurred_at,
        "created_at": occurred_at,
        "source": "manual-quote",
        "action": "QUOTE",
        "symbol": selected_symbol,
        "close": normalized_close,
        "session_date": session_date,
    }
    if selected_instrument_id:
        event["instrument_id"] = selected_instrument_id
    validate_event(event, allow_future=True)

    market_events = LedgerStore(root).read("market", repair_tail=False)
    same_key = [
        item
        for item in market_events
        if item["action"] == "QUOTE"
        and item["session_date"] == session_date
        and _quote_key(item) == _quote_key(event)
    ]
    if len(same_key) > 1:
        raise MissingQuoteError(
            "multiple existing quotes already use this instrument and session"
        )
    existing = same_key[0] if same_key else None
    if (
        existing is not None
        and Decimal(existing["close"]) != Decimal(normalized_close)
    ):
        raise MissingQuoteError(
            "an existing quote has a different close; use a reviewed "
            "correction migration instead"
        )
    benchmarks = [
        item
        for item in market_events
        if item["action"] == "BENCHMARK_CLOSE"
        and item["session_date"] == session_date
    ]
    benchmark_close = benchmarks[-1]["close"] if benchmarks else None
    return QuotePlan(
        event=event,
        existing=existing,
        benchmark_close=benchmark_close,
    )


def default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def bridge_command(root: Path, event: dict[str, Any]) -> list[str]:
    command = [
        sys.executable,
        str(BRIDGE_PATH),
        "--root",
        str(root),
        "quote",
        "--event-id",
        event["event_id"],
        "--occurred-at",
        event["occurred_at"],
        "--created-at",
        event["created_at"],
        "--session-date",
        event["session_date"],
        "--symbol",
        event["symbol"],
        "--source",
        event["source"],
        "--close",
        event["close"],
    ]
    if event.get("instrument_id"):
        command.extend(["--instrument-id", event["instrument_id"]])
    return command


def execute(
    root: Path,
    plan: QuotePlan,
    *,
    apply: bool,
    runner: Runner = default_runner,
) -> dict[str, Any]:
    if plan.existing is not None:
        return {
            "status": "current",
            "pending": 0,
            "event_id": plan.existing["event_id"],
            "session_date": plan.event["session_date"],
            "symbol": plan.event["symbol"],
            "close": plan.event["close"],
            "backup_id": None,
        }
    result: dict[str, Any] = {
        "status": "valid",
        "pending": 1,
        "event_id": plan.event["event_id"],
        "session_date": plan.event["session_date"],
        "symbol": plan.event["symbol"],
        "close": plan.event["close"],
        "benchmark_close": plan.benchmark_close,
        "backup_id": None,
    }
    if not apply:
        return result

    backup = backup_ledgers(root)
    result["backup_id"] = backup["backup_id"]
    try:
        completed = runner(bridge_command(root, plan.event))
    except subprocess.TimeoutExpired as exc:
        raise MissingQuoteError("Hermes bridge timed out") from exc
    except OSError as exc:
        raise MissingQuoteError(f"Hermes bridge could not start: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise MissingQuoteError(
            f"Hermes bridge failed with exit {completed.returncode}"
            + (f": {detail}" if detail else "")
        )
    try:
        bridge_result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MissingQuoteError("Hermes bridge returned invalid JSON") from exc
    if bridge_result.get("status") not in {
        "appended",
        "duplicate",
        "recorded_but_rebuild_pending",
    }:
        raise MissingQuoteError("Hermes bridge returned an unexpected status")
    result.update(
        {
            "status": "corrected",
            "pending": 0,
            "bridge": bridge_result,
        }
    )
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="repair-missing-quote")
    command.add_argument("--root", required=True)
    command.add_argument("--symbol", required=True)
    command.add_argument("--session-date", required=True)
    command.add_argument("--close", required=True)
    command.add_argument("--instrument-id")
    command.add_argument("--event-id")
    command.add_argument(
        "--apply",
        action="store_true",
        help="back up ledgers and append through the Hermes bridge",
    )
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.root)
    try:
        plan = build_plan(
            root,
            symbol=args.symbol,
            session_date=args.session_date,
            close=args.close,
            instrument_id=args.instrument_id,
            event_id=args.event_id,
        )
        result = execute(root.resolve(), plan, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (
        MissingQuoteError,
        OSError,
        TimeoutError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
