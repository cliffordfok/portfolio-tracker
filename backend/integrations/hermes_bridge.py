"""Safe command bridge for Hermes paper/live/market writes and reads."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from portfolio_tracker.errors import PortfolioError
from portfolio_tracker.ledger import LedgerStore, atomic_write_json
from portfolio_tracker.snapshot import build_snapshot

TRADE_COMMAND_RE = re.compile(
    r"^/trade\s+(BUY|SELL)\s+([A-Z]{1,5}(?:\.[A-Z])?)\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s+@\s*([0-9]+(?:\.[0-9]+)?)"
    r"(?:\s+fee:([0-9]+(?:\.[0-9]+)?))?"
    r"(?:\s+note:(.*))?$",
    re.IGNORECASE,
)


def parse_trade_command(text: str) -> dict[str, str]:
    match = TRADE_COMMAND_RE.fullmatch(text.strip())
    if match is None:
        raise ValueError(
            "trade command must use: /trade BUY|SELL SYMBOL SHARES @ PRICE "
            "[fee:AMOUNT] [note:TEXT]"
        )
    action, symbol, quantity, unit_price, fee, note = match.groups()
    parsed = {
        "action": action.upper(),
        "symbol": symbol.upper(),
        "shares": quantity,
        "price": unit_price,
        "fee": fee or "0",
    }
    if note and note.strip():
        parsed["note"] = note.strip()
    return parsed


def base_event(
    args: argparse.Namespace,
    action: str,
    *,
    source: str = "hermes",
) -> dict[str, Any]:
    return {
        "event_id": args.event_id,
        "portfolio": args.portfolio,
        "occurred_at": args.occurred_at,
        # Deterministic default: a retry with the same command must reproduce
        # the exact payload. Callers needing a distinct audit timestamp should
        # generate and persist the complete event before invoking LedgerStore.
        "created_at": args.occurred_at,
        "source": source,
        "action": action,
    }


def append_and_rebuild(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    result = LedgerStore(root).append(event)
    rebuild_marker = root / "state" / "rebuild.pending"
    if result["status"] == "appended" or rebuild_marker.exists():
        try:
            snapshot = build_snapshot(root)
        except (PortfolioError, ValueError, OSError) as exc:
            if result["status"] == "appended":
                result["status"] = "recorded_but_rebuild_pending"
            result["snapshot_status"] = "rebuild_pending"
            result["snapshot_error"] = str(exc)
            return result
        atomic_write_json(
            root / "state" / "publish.pending",
            {
                "revision": snapshot["revision"],
                "requested_by": "hermes-bridge",
            },
        )
        result["snapshot_revision"] = snapshot["revision"]
        result["snapshot_status"] = "rebuilt"
    return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="hermes-portfolio")
    command.add_argument("--root", required=True, help="private VPS runtime root")
    actions = command.add_subparsers(dest="command", required=True)

    open_portfolio = actions.add_parser("open")
    open_portfolio.add_argument("--portfolio", choices=("paper", "live"), required=True)
    open_portfolio.add_argument("--event-id", required=True)
    open_portfolio.add_argument("--occurred-at", required=True)
    open_portfolio.add_argument("--initial-cash", required=True)

    trade = actions.add_parser("trade")
    trade.add_argument("--portfolio", choices=("paper", "live"), required=True)
    trade.add_argument("--event-id", required=True)
    trade.add_argument("--occurred-at", required=True)
    trade.add_argument("--action", choices=("BUY", "SELL"), required=True)
    trade.add_argument("--symbol", required=True)
    trade.add_argument("--shares", required=True)
    trade.add_argument("--price", required=True)
    trade.add_argument("--fee", default="0")
    trade.add_argument("--note")
    trade.add_argument("--reason")
    trade.add_argument("--strategy")

    telegram_trade = actions.add_parser("telegram-trade")
    telegram_trade.add_argument(
        "--portfolio",
        choices=("paper", "live"),
        default="live",
    )
    telegram_trade.add_argument("--event-id", required=True)
    telegram_trade.add_argument("--occurred-at", required=True)
    telegram_trade.add_argument("--text", required=True)

    cash = actions.add_parser("cash-flow")
    cash.add_argument("--portfolio", choices=("paper", "live"), required=True)
    cash.add_argument("--event-id", required=True)
    cash.add_argument("--occurred-at", required=True)
    cash.add_argument("--amount", required=True)
    cash.add_argument("--note")

    amend = actions.add_parser("amend")
    amend.add_argument("--portfolio", choices=("paper", "live"), required=True)
    amend.add_argument("--event-id", required=True)
    amend.add_argument("--occurred-at", required=True)
    amend.add_argument("--target", required=True)
    amend.add_argument("--fee")
    amend.add_argument("--note")
    amend.add_argument("--reason")
    amend.add_argument("--strategy")

    void = actions.add_parser("void")
    void.add_argument("--portfolio", choices=("paper", "live"), required=True)
    void.add_argument("--event-id", required=True)
    void.add_argument("--occurred-at", required=True)
    void.add_argument("--target", required=True)

    quote = actions.add_parser("quote")
    quote.add_argument("--portfolio", default="market", choices=("market",))
    quote.add_argument("--event-id", required=True)
    quote.add_argument("--occurred-at", required=True)
    quote.add_argument("--session-date", required=True)
    quote.add_argument("--symbol", required=True)
    quote.add_argument("--close", required=True)
    quote.add_argument("--benchmark", action="store_true")

    read = actions.add_parser("read")
    read.add_argument("--portfolio", choices=("paper", "live"))

    rebuild = actions.add_parser("rebuild")
    rebuild.add_argument("--portfolio", default=None, help=argparse.SUPPRESS)
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == "read":
            path = root / "snapshots" / "portfolio-snapshot.json"
            if not path.exists():
                build_snapshot(root)
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            result = (
                snapshot["portfolios"][args.portfolio]
                if args.portfolio
                else snapshot
            )
        elif args.command == "rebuild":
            snapshot = build_snapshot(root)
            atomic_write_json(
                root / "state" / "publish.pending",
                {
                    "revision": snapshot["revision"],
                    "requested_by": "hermes-bridge",
                },
            )
            result = {
                "status": "rebuilt",
                "revision": snapshot["revision"],
                "warnings": snapshot["warnings"],
            }
        else:
            if args.command == "open":
                event = base_event(args, "PORTFOLIO_OPEN", source="bootstrap")
                event["initial_cash"] = args.initial_cash
                event["currency"] = "USD"
            elif args.command == "trade":
                event = base_event(args, args.action)
                event.update(
                    {
                        "symbol": args.symbol.upper(),
                        "shares": args.shares,
                        "price": args.price,
                        "fee": args.fee,
                    }
                )
                for field in ("note", "reason", "strategy"):
                    value = getattr(args, field)
                    if value:
                        event[field] = value
            elif args.command == "telegram-trade":
                parsed_trade = parse_trade_command(args.text)
                event = base_event(
                    args,
                    parsed_trade.pop("action"),
                    source="telegram",
                )
                event.update(parsed_trade)
            elif args.command == "cash-flow":
                event = base_event(args, "CASH_FLOW")
                event["amount"] = args.amount
                if args.note:
                    event["note"] = args.note
            elif args.command == "amend":
                event = base_event(args, "AMEND")
                changes = {
                    field: getattr(args, field)
                    for field in ("fee", "note", "reason", "strategy")
                    if getattr(args, field) is not None
                }
                if not changes:
                    raise ValueError("amend requires at least one mutable field")
                event.update({"amend_target": args.target, "changes": changes})
            elif args.command == "void":
                event = base_event(args, "VOID")
                event["void_target"] = args.target
            elif args.command == "quote":
                event = base_event(
                    args,
                    "BENCHMARK_CLOSE" if args.benchmark else "QUOTE",
                    source="cron-benchmark" if args.benchmark else "cron-quote",
                )
                event.update(
                    {
                        "symbol": args.symbol.upper(),
                        "close": args.close,
                        "session_date": args.session_date,
                    }
                )
            else:
                raise ValueError(f"unsupported command: {args.command}")
            result = append_and_rebuild(root, event)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except (PortfolioError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
