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
from portfolio_tracker.snapshot import build_snapshot, build_snapshot_if_needed

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
    source: str | None = None,
) -> dict[str, Any]:
    selected_source = source or getattr(args, "source", "manual-import")
    return {
        "event_id": args.event_id,
        "portfolio": args.portfolio,
        "occurred_at": args.occurred_at,
        # Deterministic default: a retry with the same command must reproduce
        # the exact payload. Callers needing a distinct audit timestamp should
        # generate and persist the complete event before invoking LedgerStore.
        "created_at": getattr(args, "created_at", None) or args.occurred_at,
        "source": selected_source,
        "action": action,
    }


def quote_batch_events(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("quote batch must be a non-empty JSON array")
    allowed_fields = {
        "event_id",
        "occurred_at",
        "created_at",
        "session_date",
        "symbol",
        "instrument_id",
        "close",
        "benchmark",
    }
    required_fields = {
        "event_id",
        "occurred_at",
        "session_date",
        "symbol",
        "close",
    }
    events: list[dict[str, Any]] = []
    sessions: set[str] = set()
    quote_keys: set[tuple[str, str]] = set()
    benchmark_count = 0

    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"quote batch item {index} must be an object")
        missing = sorted(required_fields - set(item))
        if missing:
            raise ValueError(
                f"quote batch item {index} missing: {', '.join(missing)}"
            )
        unknown = sorted(set(item) - allowed_fields)
        if unknown:
            raise ValueError(
                f"quote batch item {index} has unknown fields: "
                f"{', '.join(unknown)}"
            )
        is_benchmark = item.get("benchmark", False)
        if not isinstance(is_benchmark, bool):
            raise ValueError(
                f"quote batch item {index} benchmark must be boolean"
            )
        action = "BENCHMARK_CLOSE" if is_benchmark else "QUOTE"
        symbol = item["symbol"]
        if not isinstance(symbol, str):
            raise ValueError(f"quote batch item {index} symbol must be a string")
        symbol = symbol.upper()
        instrument_id = item.get("instrument_id")
        quote_key = (action, instrument_id or symbol)
        if quote_key in quote_keys:
            raise ValueError(
                f"quote batch contains duplicate {action} for {symbol}"
            )
        quote_keys.add(quote_key)
        session_date = item["session_date"]
        if not isinstance(session_date, str):
            raise ValueError(
                f"quote batch item {index} session_date must be a string"
            )
        sessions.add(session_date)
        if is_benchmark:
            benchmark_count += 1

        event = {
                "event_id": item["event_id"],
                "portfolio": "market",
                "occurred_at": item["occurred_at"],
                "created_at": item.get("created_at") or item["occurred_at"],
                "source": (
                    "cron-benchmark"
                    if is_benchmark
                    else "cron-quote"
                ),
                "action": action,
                "symbol": symbol,
                "close": item["close"],
                "session_date": session_date,
            }
        if instrument_id is not None:
            event["instrument_id"] = instrument_id
        events.append(event)

    if len(sessions) != 1:
        raise ValueError("quote batch must contain exactly one session_date")
    if benchmark_count != 1:
        raise ValueError("quote batch must contain exactly one benchmark event")
    return events


def load_quote_batch(path: str) -> list[dict[str, Any]]:
    payload = (
        sys.stdin.read()
        if path == "-"
        else Path(path).read_text(encoding="utf-8")
    )
    return quote_batch_events(json.loads(payload))


def _rebuild_after_write(
    root: Path,
    result: dict[str, Any],
    *,
    has_new_events: bool,
    requested_by: str,
) -> dict[str, Any]:
    rebuild_marker = root / "state" / "rebuild.pending"
    if not has_new_events and not rebuild_marker.exists():
        return result
    try:
        snapshot, rebuilt = build_snapshot_if_needed(root)
    except (PortfolioError, ValueError, OSError) as exc:
        if has_new_events:
            result["write_status"] = result["status"]
            result["status"] = "recorded_but_rebuild_pending"
        result["snapshot_status"] = "rebuild_pending"
        result["snapshot_error"] = str(exc)
        return result
    atomic_write_json(
        root / "state" / "publish.pending",
        {
            "revision": snapshot["revision"],
            "requested_by": requested_by,
        },
    )
    result["snapshot_revision"] = snapshot["revision"]
    result["snapshot_status"] = "rebuilt" if rebuilt else "current"
    return result


def append_and_rebuild(root: Path, event: dict[str, Any]) -> dict[str, Any]:
    result = LedgerStore(root).append(event)
    return _rebuild_after_write(
        root,
        result,
        has_new_events=result["status"] == "appended",
        requested_by="hermes-bridge",
    )


def append_quote_batch_and_rebuild(
    root: Path,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    result = LedgerStore(root).append_many(events)
    return _rebuild_after_write(
        root,
        result,
        has_new_events=result["appended"] > 0,
        requested_by="hermes-quote-batch",
    )


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
    trade.add_argument("--settlement-adjustment")
    trade.add_argument("--instrument-id")
    trade.add_argument(
        "--instrument-type",
        choices=("EQUITY", "ETF", "OPTION", "PRIVATE"),
    )
    trade.add_argument("--instrument-name")
    trade.add_argument("--quote-symbol")
    trade.add_argument("--contract-multiplier", default="1")
    trade.add_argument("--note")
    trade.add_argument("--reason")
    trade.add_argument("--strategy")
    trade.add_argument(
        "--source",
        choices=("manual-import", "swing-trader"),
        default="manual-import",
    )

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
    cash.add_argument(
        "--source",
        choices=("manual-import", "telegram"),
        default="manual-import",
    )

    income = actions.add_parser("income-expense")
    income.add_argument("--portfolio", choices=("paper", "live"), required=True)
    income.add_argument("--event-id", required=True)
    income.add_argument("--occurred-at", required=True)
    income.add_argument("--symbol", required=True)
    income.add_argument("--instrument-id")
    income.add_argument("--instrument-name")
    income.add_argument("--amount", required=True)
    income.add_argument("--gross-amount")
    income.add_argument("--withholding-tax", default="0")
    income.add_argument(
        "--income-type",
        choices=("DIVIDEND", "INTEREST", "FEE", "CASH_IN_LIEU", "OTHER"),
        required=True,
    )
    income.add_argument("--note")
    income.add_argument(
        "--source",
        choices=("manual-import", "telegram"),
        default="manual-import",
    )

    split = actions.add_parser("split")
    split.add_argument("--portfolio", choices=("paper", "live"), required=True)
    split.add_argument("--event-id", required=True)
    split.add_argument("--occurred-at", required=True)
    split.add_argument("--symbol", required=True)
    split.add_argument("--instrument-id", required=True)
    split.add_argument(
        "--instrument-type",
        choices=("EQUITY", "ETF", "PRIVATE"),
        default="EQUITY",
    )
    split.add_argument("--instrument-name")
    split.add_argument("--quote-symbol")
    split.add_argument("--numerator", required=True)
    split.add_argument("--denominator", required=True)
    split.add_argument("--note")
    split.add_argument(
        "--source",
        choices=("manual-import",),
        default="manual-import",
    )

    amend = actions.add_parser("amend")
    amend.add_argument("--portfolio", choices=("paper", "live"), required=True)
    amend.add_argument("--event-id", required=True)
    amend.add_argument("--occurred-at", required=True)
    amend.add_argument("--target", required=True)
    amend.add_argument("--fee")
    amend.add_argument("--settlement-adjustment")
    amend.add_argument("--note")
    amend.add_argument("--reason")
    amend.add_argument("--strategy")
    amend.add_argument("--amend-reason", required=True)
    amend.add_argument(
        "--source",
        choices=("manual-import", "telegram"),
        default="manual-import",
    )

    void = actions.add_parser("void")
    void.add_argument("--portfolio", choices=("paper", "live"), required=True)
    void.add_argument("--event-id", required=True)
    void.add_argument("--occurred-at", required=True)
    void.add_argument("--target", required=True)
    void.add_argument("--void-reason", required=True)
    void.add_argument(
        "--source",
        choices=("manual-import", "telegram"),
        default="manual-import",
    )

    quote = actions.add_parser("quote")
    quote.add_argument("--portfolio", default="market", choices=("market",))
    quote.add_argument("--event-id", required=True)
    quote.add_argument("--occurred-at", required=True)
    quote.add_argument("--session-date", required=True)
    quote.add_argument("--symbol", required=True)
    quote.add_argument("--instrument-id")
    quote.add_argument("--close", required=True)
    quote.add_argument("--benchmark", action="store_true")
    quote.add_argument(
        "--source",
        choices=("cron-quote", "manual-quote"),
        default="manual-quote",
    )

    quote_batch = actions.add_parser("quote-batch")
    quote_batch.add_argument(
        "--file",
        required=True,
        help="UTF-8 JSON array path, or - to read from stdin",
    )

    for event_parser in (
        open_portfolio,
        trade,
        telegram_trade,
        cash,
        income,
        split,
        amend,
        void,
        quote,
    ):
        event_parser.add_argument(
            "--created-at",
            help=(
                "UTC recording time; persist and reuse this exact value on retry "
                "(defaults to occurred-at)"
            ),
        )

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
            snapshot, rebuilt = build_snapshot_if_needed(root)
            if rebuilt:
                atomic_write_json(
                    root / "state" / "publish.pending",
                    {
                        "revision": snapshot["revision"],
                        "requested_by": "hermes-read-recovery",
                    },
                )
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
        elif args.command == "quote-batch":
            result = append_quote_batch_and_rebuild(
                root,
                load_quote_batch(args.file),
            )
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
                if args.settlement_adjustment is not None:
                    event["settlement_adjustment"] = args.settlement_adjustment
                for field in ("note", "reason", "strategy"):
                    value = getattr(args, field)
                    if value:
                        event[field] = value
                for field in (
                    "instrument_id",
                    "instrument_type",
                    "instrument_name",
                    "quote_symbol",
                ):
                    value = getattr(args, field)
                    if value:
                        event[field] = value
                if args.contract_multiplier != "1":
                    event["contract_multiplier"] = args.contract_multiplier
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
                event["symbol"] = "USD"
                if args.note:
                    event["note"] = args.note
            elif args.command == "income-expense":
                event = base_event(args, "INCOME_EXPENSE")
                event.update(
                    {
                        "symbol": args.symbol.upper(),
                        "amount": args.amount,
                        "withholding_tax": args.withholding_tax,
                        "income_type": args.income_type,
                    }
                )
                for field in (
                    "instrument_id",
                    "instrument_name",
                    "gross_amount",
                    "note",
                ):
                    value = getattr(args, field)
                    if value is not None:
                        event[field] = value
            elif args.command == "split":
                event = base_event(args, "SPLIT")
                event.update(
                    {
                        "symbol": args.symbol.upper(),
                        "instrument_id": args.instrument_id,
                        "instrument_type": args.instrument_type,
                        "numerator": args.numerator,
                        "denominator": args.denominator,
                    }
                )
                for field in ("instrument_name", "quote_symbol", "note"):
                    value = getattr(args, field)
                    if value:
                        event[field] = value
            elif args.command == "amend":
                event = base_event(args, "AMEND")
                changes = {
                    field: getattr(args, field)
                    for field in (
                        "fee",
                        "settlement_adjustment",
                        "note",
                        "reason",
                        "strategy",
                    )
                    if getattr(args, field) is not None
                }
                if not changes:
                    raise ValueError("amend requires at least one mutable field")
                event.update(
                    {
                        "amend_target": args.target,
                        "changes": changes,
                        "amend_reason": args.amend_reason,
                    }
                )
            elif args.command == "void":
                event = base_event(args, "VOID")
                event["void_target"] = args.target
                event["void_reason"] = args.void_reason
            elif args.command == "quote":
                event = base_event(
                    args,
                    "BENCHMARK_CLOSE" if args.benchmark else "QUOTE",
                    source="cron-benchmark" if args.benchmark else args.source,
                )
                event.update(
                    {
                        "symbol": args.symbol.upper(),
                        "close": args.close,
                        "session_date": args.session_date,
                    }
                )
                if args.instrument_id:
                    event["instrument_id"] = args.instrument_id
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
