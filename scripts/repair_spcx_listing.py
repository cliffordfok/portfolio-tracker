"""Append-only correction for the 2026 SPCX public listing classification."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from portfolio_tracker.backup import backup_ledgers
from portfolio_tracker.errors import ConflictError, PortfolioError
from portfolio_tracker.ledger import LedgerStore, atomic_write_json
from portfolio_tracker.replay import ReplayResult, replay_portfolio
from portfolio_tracker.resolver import resolve_effective_events
from portfolio_tracker.schemas import ACTION_FIELDS, normalize_event, validate_event
from portfolio_tracker.snapshot import build_snapshot_if_needed


MIGRATION_CREATED_AT = "2026-07-26T16:00:00Z"
OLD_INSTRUMENT_ID = "PRIVATE:SPACEX"
NEW_INSTRUMENT_ID = "EQUITY:SPCX"
SPCX_SYMBOL = "SPCX"
EXPECTED_EFFECTIVE_DATE = "2026-07-22"
EXPECTED_LATEST_CLOSE = "115.07"

MARKET_CLOSES: dict[str, dict[str, str]] = {
    "2026-07-22": {
        "MU": "959.48",
        "NVDA": "212.06",
        "VOO": "687.03",
        "SKHYV": "165.27",
        "SPCX": "115.26",
        "SPY": "747.41",
    },
    "2026-07-23": {
        "MU": "990.21",
        "NVDA": "208.76",
        "VOO": "678.61",
        "SKHYV": "169.50",
        "SPCX": "118.24",
        "SPY": "738.18",
    },
    "2026-07-24": {
        "MU": "920.95",
        "NVDA": "206.84",
        "VOO": "679.14",
        "SKHYV": "154.57",
        "SPCX": "115.07",
        "SPY": "738.93",
    },
}


class SpcxMigrationError(ValueError):
    """Raised when the SPCX correction cannot be applied safely."""


@dataclass
class MigrationPlan:
    candidates: list[dict[str, Any]]
    pending: int
    duplicates: int
    quote_candidates: int
    target_event_id: str
    already_corrected: bool


def _without_sequence(event: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(event)
    value.pop("ledger_seq", None)
    return normalize_event(value)


def _event_suffix(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]


def _correction_events(target: dict[str, Any]) -> list[dict[str, Any]]:
    suffix = _event_suffix(target["event_id"])
    replacement = {
        "event_id": f"live-spcx-listing-buy-{suffix}",
        "portfolio": "live",
        "occurred_at": target["occurred_at"],
        "created_at": MIGRATION_CREATED_AT,
        "source": "manual-import",
        "action": "BUY",
    }
    for field in ACTION_FIELDS["BUY"]:
        if field in target:
            replacement[field] = deepcopy(target[field])
    replacement.update(
        {
            "symbol": SPCX_SYMBOL,
            "instrument_id": NEW_INSTRUMENT_ID,
            "instrument_type": "EQUITY",
            "instrument_name": "Space Exploration Technologies Corp.",
            "quote_symbol": SPCX_SYMBOL,
        }
    )
    cancellation = {
        "event_id": f"live-spcx-listing-void-{suffix}",
        "portfolio": "live",
        "occurred_at": MIGRATION_CREATED_AT,
        "created_at": MIGRATION_CREATED_AT,
        "source": "manual-import",
        "action": "VOID",
        "void_target": target["event_id"],
        "void_reason": (
            "Correct SPCX from pre-listing private classification to its "
            "public Nasdaq equity identity"
        ),
    }
    return [replacement, cancellation]


def _market_event(
    *,
    session_date: str,
    symbol: str,
    close: str,
) -> dict[str, Any]:
    benchmark = symbol == "SPY"
    label = "spy-benchmark" if benchmark else symbol.lower()
    return {
        "event_id": f"market-ibkr-backfill-{label}-{session_date}",
        "portfolio": "market",
        "occurred_at": f"{session_date}T20:00:00Z",
        "created_at": MIGRATION_CREATED_AT,
        "source": "cron-benchmark" if benchmark else "cron-quote",
        "action": "BENCHMARK_CLOSE" if benchmark else "QUOTE",
        "symbol": symbol,
        "close": close,
        "session_date": session_date,
    }


def _existing_event_map(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = event["event_id"]
        if event_id in result:
            raise SpcxMigrationError(
                f"existing ledger has duplicate event_id: {event_id}"
            )
        result[event_id] = event
    return result


def _simulate(
    existing: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    existing_by_id = _existing_event_map(existing)
    proposed = [deepcopy(event) for event in existing]
    next_sequence = max(
        (int(event["ledger_seq"]) for event in existing),
        default=0,
    ) + 1
    pending = 0
    duplicates = 0
    for candidate in candidates:
        validate_event(candidate)
        stored = existing_by_id.get(candidate["event_id"])
        if stored is not None:
            if _without_sequence(stored) != normalize_event(candidate):
                raise ConflictError(
                    "event_id already exists with different payload: "
                    f"{candidate['event_id']}"
                )
            duplicates += 1
            continue
        item = normalize_event(candidate)
        item["ledger_seq"] = next_sequence
        next_sequence += 1
        proposed.append(item)
        existing_by_id[item["event_id"]] = item
        pending += 1
    return proposed, pending, duplicates


def _financial_invariants(result: ReplayResult) -> dict[str, Any]:
    return {
        "initial_cash": result.initial_cash,
        "cash": result.cash,
        "cash_flow_total": result.cash_flow_total,
        "income_expense_total": result.income_expense_total,
        "buy_outflow": result.buy_outflow,
        "sell_inflow": result.sell_inflow,
        "realized_pnl_total": result.realized_pnl_total,
        "trade_count": len(result.trade_history),
        "realized_trade_count": len(result.realized_pnl_per_trade),
        "closed_episode_count": len(result.closed_episodes),
    }


def _holding_map(result: ReplayResult) -> dict[str, dict[str, Any]]:
    return {
        holding["instrument_id"]: deepcopy(holding)
        for holding in result.holdings
    }


def _assert_reclassification_invariants(
    before: ReplayResult,
    after: ReplayResult,
) -> None:
    if _financial_invariants(before) != _financial_invariants(after):
        raise SpcxMigrationError(
            "SPCX correction would change portfolio financial invariants"
        )
    before_holdings = _holding_map(before)
    after_holdings = _holding_map(after)
    old_holding = before_holdings.pop(OLD_INSTRUMENT_ID, None)
    new_holding = after_holdings.pop(NEW_INSTRUMENT_ID, None)
    if old_holding is None or new_holding is None:
        raise SpcxMigrationError(
            "SPCX correction must replace exactly one open instrument identity"
        )
    if before_holdings != after_holdings:
        raise SpcxMigrationError(
            "SPCX correction would change an unrelated holding"
        )
    for field in (
        "symbol",
        "shares",
        "avg_cost",
        "cost_basis",
        "contract_multiplier",
    ):
        if old_holding[field] != new_holding[field]:
            raise SpcxMigrationError(
                f"SPCX correction would change holding field: {field}"
            )
    if (
        new_holding["instrument_id"] != NEW_INSTRUMENT_ID
        or new_holding["instrument_type"] != "EQUITY"
        or new_holding["quote_symbol"] != SPCX_SYMBOL
    ):
        raise SpcxMigrationError("SPCX corrected holding metadata is invalid")


def _assert_already_corrected(result: ReplayResult) -> None:
    holdings = _holding_map(result)
    if OLD_INSTRUMENT_ID in holdings:
        raise SpcxMigrationError("old SPCX private holding remains active")
    holding = holdings.get(NEW_INSTRUMENT_ID)
    if (
        holding is None
        or holding["instrument_type"] != "EQUITY"
        or holding["quote_symbol"] != SPCX_SYMBOL
    ):
        raise SpcxMigrationError("corrected SPCX equity holding is missing")


def _quote_candidates(
    market_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in market_events:
        action = event["action"]
        if action not in {"QUOTE", "BENCHMARK_CLOSE"}:
            continue
        key = event.get("instrument_id") or event["symbol"]
        latest[(action, key, event["session_date"])] = event

    candidates: list[dict[str, Any]] = []
    for session_date, closes in MARKET_CLOSES.items():
        for symbol, close in closes.items():
            candidate = _market_event(
                session_date=session_date,
                symbol=symbol,
                close=close,
            )
            action = candidate["action"]
            key = candidate["symbol"]
            existing = latest.get((action, key, session_date))
            if existing is not None:
                if Decimal(existing["close"]) != Decimal(close):
                    raise SpcxMigrationError(
                        "conflicting market close for "
                        f"{symbol} on {session_date}"
                    )
                continue
            candidates.append(candidate)
    return candidates


def inspect_migration(root: Path) -> MigrationPlan:
    store = LedgerStore(root)
    live_events = store.read("live", repair_tail=False)
    market_events = store.read("market", repair_tail=False)
    if not live_events:
        raise SpcxMigrationError("Live ledger is not initialized")

    raw_targets = [
        event
        for event in live_events
        if event["action"] == "BUY"
        and event.get("symbol") == SPCX_SYMBOL
        and event.get("instrument_id") == OLD_INSTRUMENT_ID
    ]
    if len(raw_targets) != 1:
        raise SpcxMigrationError(
            "expected exactly one raw PRIVATE:SPACEX BUY event"
        )
    target = raw_targets[0]
    correction_events = _correction_events(target)
    existing_by_id = _existing_event_map(live_events + market_events)
    correction_presence = [
        event["event_id"] in existing_by_id
        for event in correction_events
    ]
    if any(correction_presence) and not all(correction_presence):
        raise SpcxMigrationError("SPCX correction is only partially recorded")
    already_corrected = all(correction_presence)

    effective_live = resolve_effective_events(live_events)
    active_old = [
        event
        for event in effective_live
        if event.get("instrument_id") == OLD_INSTRUMENT_ID
    ]
    if already_corrected:
        if active_old:
            raise SpcxMigrationError(
                "recorded SPCX correction did not retire the private identity"
            )
    elif active_old != [target]:
        raise SpcxMigrationError(
            "PRIVATE:SPACEX has unsupported dependent or duplicate events"
        )

    before = replay_portfolio(live_events, portfolio="live")
    proposed_live, correction_pending, correction_duplicates = _simulate(
        live_events,
        correction_events,
    )
    after = replay_portfolio(proposed_live, portfolio="live")
    if already_corrected:
        _assert_already_corrected(after)
    else:
        _assert_reclassification_invariants(before, after)

    quote_candidates = _quote_candidates(market_events)
    proposed_market, quote_pending, quote_duplicates = _simulate(
        market_events,
        quote_candidates,
    )
    resolve_effective_events(proposed_market)

    candidates = correction_events + quote_candidates
    return MigrationPlan(
        candidates=candidates,
        pending=correction_pending + quote_pending,
        duplicates=correction_duplicates + quote_duplicates,
        quote_candidates=len(quote_candidates),
        target_event_id=target["event_id"],
        already_corrected=already_corrected,
    )


def _verify_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    live = snapshot["portfolios"]["live"]
    holdings = {
        holding["instrument_id"]: holding
        for holding in live["holdings"]
    }
    if OLD_INSTRUMENT_ID in holdings:
        raise SpcxMigrationError("published snapshot still has private SPCX")
    spcx = holdings.get(NEW_INSTRUMENT_ID)
    if spcx is None:
        raise SpcxMigrationError("published snapshot is missing equity SPCX")
    if (
        spcx["quote_symbol"] != SPCX_SYMBOL
        or spcx["quote_status"] != "OK"
        or Decimal(spcx["current_price"]) != Decimal(EXPECTED_LATEST_CLOSE)
    ):
        raise SpcxMigrationError("published snapshot SPCX quote is invalid")
    metrics = live["metrics"]
    if (
        metrics["data_status"] != "OK"
        or metrics["performance_effective_date"] != EXPECTED_EFFECTIVE_DATE
        or metrics["performance_scope"] != "LATEST_COMPLETE_SEGMENT"
    ):
        raise SpcxMigrationError(
            "Live performance did not recover from the expected date"
        )
    return {
        "instrument_id": spcx["instrument_id"],
        "instrument_type": spcx["instrument_type"],
        "quote_symbol": spcx["quote_symbol"],
        "quote_status": spcx["quote_status"],
        "current_price": spcx["current_price"],
        "performance_effective_date": metrics["performance_effective_date"],
        "performance_scope": metrics["performance_scope"],
    }


def execute(*, root: Path, apply: bool) -> dict[str, Any]:
    plan = inspect_migration(root)
    response: dict[str, Any] = {
        "status": "valid" if not apply else "ready",
        "migration": "spcx-public-listing-2026",
        "target_events": 1,
        "quote_events": plan.quote_candidates,
        "pending": plan.pending,
        "duplicates": plan.duplicates,
        "already_corrected": plan.already_corrected,
    }
    if not apply:
        return response
    if plan.pending == 0:
        snapshot, rebuilt = build_snapshot_if_needed(root)
        response.update(
            {
                "status": "current",
                "snapshot_revision": snapshot["revision"],
                "snapshot_status": "rebuilt" if rebuilt else "current",
                "acceptance": _verify_snapshot(snapshot),
            }
        )
        return response

    backup = backup_ledgers(root)
    plan = inspect_migration(root)
    if plan.pending == 0:
        snapshot, rebuilt = build_snapshot_if_needed(root)
        response.update(
            {
                "status": "current",
                "backup_id": backup["backup_id"],
                "snapshot_revision": snapshot["revision"],
                "snapshot_status": "rebuilt" if rebuilt else "current",
                "acceptance": _verify_snapshot(snapshot),
            }
        )
        return response

    batch = LedgerStore(root).append_many(plan.candidates)
    snapshot, rebuilt = build_snapshot_if_needed(root)
    acceptance = _verify_snapshot(snapshot)
    atomic_write_json(
        root / "state" / "publish.pending",
        {
            "revision": snapshot["revision"],
            "requested_by": "spcx-public-listing-migration",
        },
    )
    response.update(
        {
            "status": "corrected",
            "backup_id": backup["backup_id"],
            "appended": batch["appended"],
            "duplicates": batch["duplicates"],
            "snapshot_revision": snapshot["revision"],
            "snapshot_status": "rebuilt" if rebuilt else "current",
            "acceptance": acceptance,
        }
    )
    return response


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="repair-spcx-listing")
    command.add_argument("--root", required=True)
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check-only", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(
            root=Path(args.root).resolve(),
            apply=args.apply,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        ConflictError,
        PortfolioError,
        SpcxMigrationError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {"status": "error", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
