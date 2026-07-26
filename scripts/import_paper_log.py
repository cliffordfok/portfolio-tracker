#!/usr/bin/env python3
"""Import the legacy paper trading JSONL log through Hermes' safe bridge.

This is a migration/reconciliation tool, not the ongoing writer. Future paper
trades must carry an immutable event ID in the trader's durable outbox and call
``hermes_bridge.py`` directly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Sequence


DEFAULT_CUTOFF = "2026-07-16T00:00:00Z"
DEFAULT_SOURCE_LOG = Path("/data/scripts/paper_trading_log.jsonl")
DEFAULT_RUNTIME_ROOT = Path("/data/portfolio")
DEFAULT_BRIDGE = (
    Path(__file__).resolve().parents[1]
    / "backend"
    / "integrations"
    / "hermes_bridge.py"
)
# ``pnl``, ``pnl_pct``, ``proceeds`` and ``remaining_shares`` are legacy SELL
# log output only.
# They are deliberately accepted for reconciliation but never hashed or copied
# into master ledger events, where all P&L must be derived by FIFO replay.
ALLOWED_SOURCE_FIELDS = {
    "action",
    "cost",
    "fee",
    "note",
    "pnl",
    "pnl_pct",
    "price",
    "proceeds",
    "reason",
    "remaining_shares",
    "shares",
    "strategy",
    "symbol",
    "timestamp",
}
REQUIRED_SOURCE_FIELDS = {"action", "price", "shares", "symbol", "timestamp"}
ACTION_MAP = {"BUY": "BUY", "SELL": "SELL", "SELL_PARTIAL": "SELL"}


class PaperLogImportError(ValueError):
    """Raised when preflight validation or a bridge call fails."""


@dataclass(frozen=True)
class PreparedTrade:
    event_id: str
    occurred_at: str
    action: str
    symbol: str
    shares: str
    price: str
    fee: str
    reason: str | None
    strategy: str | None


@dataclass(frozen=True)
class ImportPlan:
    source_events: int
    skipped_before_cutoff: int
    trades: tuple[PreparedTrade, ...]


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def parse_utc(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PaperLogImportError(f"{field} must be a non-empty timestamp")
    candidate = value.strip()
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PaperLogImportError(f"{field} must be an ISO8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utc_text(value: Any, *, field: str) -> str:
    parsed = parse_utc(value, field=field)
    return parsed.isoformat().replace("+00:00", "Z")


def decimal_text(value: Any, *, field: str, positive: bool) -> str:
    if isinstance(value, bool):
        raise PaperLogImportError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaperLogImportError(f"{field} must be numeric") from exc
    if not parsed.is_finite():
        raise PaperLogImportError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise PaperLogImportError(f"{field} must be greater than zero")
    if not positive and parsed < 0:
        raise PaperLogImportError(f"{field} must be non-negative")
    # Preserve the source JSON number spelling used by the original importer.
    # This is required so retries reproduce the 71 already-imported event IDs.
    return str(value)


def legacy_event_id(event: dict[str, Any]) -> str:
    """Reproduce the event ID rule used for the completed Stage A backfill."""

    action = ACTION_MAP.get(str(event.get("action", "")).upper())
    if action is None:
        raise PaperLogImportError("action must be BUY, SELL, or SELL_PARTIAL")
    parts = (
        str(event["timestamp"]),
        action,
        str(event["symbol"]).upper(),
        str(event["shares"]),
        str(event["price"]),
        str(event.get("fee", 0)),
        str(event.get("strategy", "")),
        str(event.get("reason", "")),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"paper-swing-{digest}"


def prepare_trade(event: Any, *, line_number: int) -> PreparedTrade:
    if not isinstance(event, dict):
        raise PaperLogImportError(f"line {line_number}: event must be an object")
    missing = sorted(REQUIRED_SOURCE_FIELDS - set(event))
    if missing:
        raise PaperLogImportError(
            f"line {line_number}: missing fields: {', '.join(missing)}"
        )
    unknown = sorted(set(event) - ALLOWED_SOURCE_FIELDS)
    if unknown:
        raise PaperLogImportError(
            f"line {line_number}: unknown fields: {', '.join(unknown)}"
        )

    raw_action = event["action"]
    if not isinstance(raw_action, str):
        raise PaperLogImportError(f"line {line_number}: action must be a string")
    action = ACTION_MAP.get(raw_action.upper())
    if action is None:
        raise PaperLogImportError(
            f"line {line_number}: action must be BUY, SELL, or SELL_PARTIAL"
        )

    symbol = event["symbol"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise PaperLogImportError(
            f"line {line_number}: symbol must be a non-empty string"
        )
    symbol = symbol.strip().upper()
    reason = event.get("reason")
    strategy = event.get("strategy")
    for field, value in (("reason", reason), ("strategy", strategy)):
        if value is not None and not isinstance(value, str):
            raise PaperLogImportError(
                f"line {line_number}: {field} must be a string"
            )

    return PreparedTrade(
        event_id=legacy_event_id(event),
        occurred_at=utc_text(event["timestamp"], field=f"line {line_number} timestamp"),
        action=action,
        symbol=symbol,
        shares=decimal_text(
            event["shares"],
            field=f"line {line_number} shares",
            positive=True,
        ),
        price=decimal_text(
            event["price"],
            field=f"line {line_number} price",
            positive=True,
        ),
        fee=decimal_text(
            event.get("fee", 0),
            field=f"line {line_number} fee",
            positive=False,
        ),
        reason=reason or None,
        strategy=strategy or None,
    )


def build_plan(source_log: Path, *, cutoff: str) -> ImportPlan:
    cutoff_at = parse_utc(cutoff, field="cutoff")
    source_events = 0
    skipped = 0
    prepared: list[PreparedTrade] = []
    previous_at: datetime | None = None
    seen: dict[str, PreparedTrade] = {}

    try:
        lines = source_log.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PaperLogImportError(f"cannot read source log: {source_log}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        source_events += 1
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PaperLogImportError(f"line {line_number}: invalid JSON") from exc
        if not isinstance(raw, dict):
            raise PaperLogImportError(
                f"line {line_number}: event must be an object"
            )
        occurred_at = parse_utc(
            raw.get("timestamp"),
            field=f"line {line_number} timestamp",
        )
        if occurred_at < cutoff_at:
            skipped += 1
            continue
        trade = prepare_trade(raw, line_number=line_number)
        if previous_at is not None and occurred_at < previous_at:
            raise PaperLogImportError(
                f"line {line_number}: post-cutoff timestamps must be ordered"
            )
        previous_at = occurred_at
        existing = seen.get(trade.event_id)
        if existing is not None:
            if existing != trade:
                raise PaperLogImportError(
                    f"line {line_number}: event ID collision in source log"
                )
            continue
        seen[trade.event_id] = trade
        prepared.append(trade)

    return ImportPlan(source_events, skipped, tuple(prepared))


def expected_ledger_payload(trade: PreparedTrade) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "event_id": trade.event_id,
        "portfolio": "paper",
        "occurred_at": trade.occurred_at,
        "created_at": trade.occurred_at,
        "source": "swing-trader",
        "action": trade.action,
        "symbol": trade.symbol,
        "shares": trade.shares,
        "price": trade.price,
        "fee": trade.fee,
    }
    if trade.reason is not None:
        payload["reason"] = trade.reason
    if trade.strategy is not None:
        payload["strategy"] = trade.strategy
    return payload


def reconcile_ledger(plan: ImportPlan, ledger_path: Path) -> dict[str, int]:
    """Fail before writes if an earlier legacy import does not match the plan."""

    planned = {trade.event_id: trade for trade in plan.trades}
    matched: set[str] = set()
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {"existing": 0, "missing": len(planned)}
    except OSError as exc:
        raise PaperLogImportError(f"cannot read paper ledger: {ledger_path}") from exc

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PaperLogImportError(
                f"paper ledger line {line_number}: invalid JSON"
            ) from exc
        if not isinstance(event, dict):
            raise PaperLogImportError(
                f"paper ledger line {line_number}: event must be an object"
            )
        event_id = event.get("event_id")
        if (
            event.get("source") != "swing-trader"
            or not isinstance(event_id, str)
            or not event_id.startswith("paper-swing-")
        ):
            continue
        trade = planned.get(event_id)
        if trade is None:
            raise PaperLogImportError(
                "paper ledger contains a legacy swing-trader event "
                "that is absent from the source plan"
            )
        expected = expected_ledger_payload(trade)
        actual = {
            field: value
            for field, value in event.items()
            if field != "ledger_seq"
        }
        if actual != expected:
            raise PaperLogImportError(
                f"paper ledger payload mismatch for event {event_id}"
            )
        matched.add(event_id)

    return {"existing": len(matched), "missing": len(planned) - len(matched)}


def bridge_command(
    trade: PreparedTrade,
    *,
    python: str,
    bridge: Path,
    runtime_root: Path,
) -> list[str]:
    command = [
        python,
        str(bridge),
        "--root",
        str(runtime_root),
        "trade",
        "--portfolio",
        "paper",
        "--event-id",
        trade.event_id,
        "--occurred-at",
        trade.occurred_at,
        "--action",
        trade.action,
        "--symbol",
        trade.symbol,
        "--shares",
        trade.shares,
        "--price",
        trade.price,
        "--fee",
        trade.fee,
        "--source",
        "swing-trader",
    ]
    if trade.reason is not None:
        command.extend(("--reason", trade.reason))
    if trade.strategy is not None:
        command.extend(("--strategy", trade.strategy))
    return command


def run_bridge(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def execute_plan(
    plan: ImportPlan,
    *,
    python: str,
    bridge: Path,
    runtime_root: Path,
    runner: Runner = run_bridge,
) -> dict[str, int | str]:
    appended = 0
    duplicates = 0
    rebuild_pending = 0
    for trade in plan.trades:
        completed = runner(
            bridge_command(
                trade,
                python=python,
                bridge=bridge,
                runtime_root=runtime_root,
            )
        )
        if completed.returncode != 0:
            raise PaperLogImportError(
                f"bridge rejected event {trade.event_id} "
                f"(exit {completed.returncode})"
            )
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PaperLogImportError(
                f"bridge returned invalid JSON for event {trade.event_id}"
            ) from exc
        status = result.get("status") if isinstance(result, dict) else None
        if status == "appended":
            appended += 1
        elif status == "duplicate":
            duplicates += 1
        elif status == "recorded_but_rebuild_pending":
            appended += 1
            rebuild_pending += 1
        else:
            raise PaperLogImportError(
                f"bridge returned unsupported status for event {trade.event_id}"
            )
    return {
        "status": "ok",
        "source_events": plan.source_events,
        "skipped_before_cutoff": plan.skipped_before_cutoff,
        "eligible": len(plan.trades),
        "appended": appended,
        "duplicates": duplicates,
        "rebuild_pending": rebuild_pending,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Import legacy paper trades through hermes_bridge.py"
    )
    command.add_argument("--source-log", type=Path, default=DEFAULT_SOURCE_LOG)
    command.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    command.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    command.add_argument("--python", default=sys.executable)
    command.add_argument("--cutoff", default=DEFAULT_CUTOFF)
    command.add_argument(
        "--check-only",
        action="store_true",
        help="preflight the complete source without writing any ledger event",
    )
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        plan = build_plan(args.source_log, cutoff=args.cutoff)
        ledger_path = args.runtime_root / "ledger" / "paper.jsonl"
        reconciliation = reconcile_ledger(plan, ledger_path)
        if args.check_only:
            result: dict[str, int | str] = {
                "status": "valid",
                "source_events": plan.source_events,
                "skipped_before_cutoff": plan.skipped_before_cutoff,
                "eligible": len(plan.trades),
                "existing": reconciliation["existing"],
                "missing": reconciliation["missing"],
            }
        else:
            if not args.bridge.is_file():
                raise PaperLogImportError(f"bridge not found: {args.bridge}")
            result = execute_plan(
                plan,
                python=args.python,
                bridge=args.bridge,
                runtime_root=args.runtime_root,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (PaperLogImportError, OSError, subprocess.SubprocessError) as exc:
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
