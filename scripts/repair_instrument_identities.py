"""Append-only correction for CBRS and the SKHY ticker transition."""

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


MIGRATION_CREATED_AT = "2026-07-27T14:40:00Z"
CBRS_OLD_ID = "PRIVATE:CEREBRAS"
CBRS_NEW_ID = "EQUITY:CBRS"
SKHY_OLD_ID = "EQUITY:SKHYV"
SKHY_NEW_ID = "EQUITY:SKHY"
SKHY_SESSIONS = ("2026-07-22", "2026-07-23", "2026-07-24")
EXPECTED_SKHY_SHARES = Decimal("10")
EXPECTED_SKHY_AVG_COST = Decimal("170")
EXPECTED_SKHY_LATEST_CLOSE = Decimal("154.57")
EXPECTED_EFFECTIVE_DATE = "2026-07-22"


class IdentityMigrationError(ValueError):
    """Raised when an identity correction cannot be applied safely."""


@dataclass
class MigrationPlan:
    candidates: list[dict[str, Any]]
    pending: int
    duplicates: int
    target_events: int
    quote_events: int
    already_corrected: bool


def _without_sequence(event: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(event)
    value.pop("ledger_seq", None)
    return normalize_event(value)


def _event_suffix(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]


def _replacement_event(
    target: dict[str, Any],
    *,
    symbol: str,
    instrument_id: str,
    instrument_name: str,
    label: str,
) -> dict[str, Any]:
    suffix = _event_suffix(target["event_id"])
    replacement = {
        "event_id": f"live-identity-{label}-{target['action'].lower()}-{suffix}",
        "portfolio": "live",
        "occurred_at": target["occurred_at"],
        "created_at": MIGRATION_CREATED_AT,
        "source": "manual-import",
        "action": target["action"],
    }
    for field in ACTION_FIELDS[target["action"]]:
        if field in target:
            replacement[field] = deepcopy(target[field])
    replacement.update(
        {
            "symbol": symbol,
            "instrument_id": instrument_id,
            "instrument_type": "EQUITY",
            "instrument_name": instrument_name,
            "quote_symbol": symbol,
        }
    )
    return replacement


def _void_event(target: dict[str, Any], *, label: str) -> dict[str, Any]:
    suffix = _event_suffix(target["event_id"])
    return {
        "event_id": f"live-identity-{label}-void-{suffix}",
        "portfolio": "live",
        "occurred_at": MIGRATION_CREATED_AT,
        "created_at": MIGRATION_CREATED_AT,
        "source": "manual-import",
        "action": "VOID",
        "void_target": target["event_id"],
        "void_reason": (
            "Correct the instrument identity after authoritative public "
            "listing and ticker verification"
        ),
    }


def _correction_events(
    targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for target in targets:
        if target["instrument_id"] == CBRS_OLD_ID:
            replacement = _replacement_event(
                target,
                symbol="CBRS",
                instrument_id=CBRS_NEW_ID,
                instrument_name="Cerebras Systems Inc.",
                label="cbrs-public",
            )
            label = "cbrs-public"
        elif target["instrument_id"] == SKHY_OLD_ID:
            replacement = _replacement_event(
                target,
                symbol="SKHY",
                instrument_id=SKHY_NEW_ID,
                instrument_name="SK hynix Inc. ADR",
                label="skhy-symbol",
            )
            label = "skhy-symbol"
        else:
            raise IdentityMigrationError("unsupported migration target")
        candidates.extend([replacement, _void_event(target, label=label)])
    return candidates


def _market_quote_event(target: dict[str, Any]) -> dict[str, Any]:
    suffix = _event_suffix(target["event_id"])
    return {
        "event_id": f"market-skhy-symbol-quote-{suffix}",
        "portfolio": "market",
        "occurred_at": target["occurred_at"],
        "created_at": MIGRATION_CREATED_AT,
        "source": target["source"],
        "action": "QUOTE",
        "symbol": "SKHY",
        "close": target["close"],
        "session_date": target["session_date"],
    }


def _existing_event_map(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = event["event_id"]
        if event_id in result:
            raise IdentityMigrationError(
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


def _target_events(
    live_events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cbrs = [
        event
        for event in live_events
        if event["action"] in {"BUY", "SELL"}
        and event.get("symbol") == "CBRS"
        and event.get("instrument_id") == CBRS_OLD_ID
    ]
    skhy = [
        event
        for event in live_events
        if event["action"] in {"BUY", "SELL"}
        and event.get("symbol") == "SKHYV"
        and event.get("instrument_id") == SKHY_OLD_ID
    ]
    if (
        len(cbrs) != 5
        or sum(event["action"] == "BUY" for event in cbrs) != 4
        or sum(event["action"] == "SELL" for event in cbrs) != 1
    ):
        raise IdentityMigrationError(
            "expected four CBRS BUY events and one CBRS SELL event"
        )
    if len(skhy) != 1 or skhy[0]["action"] != "BUY":
        raise IdentityMigrationError("expected exactly one SKHYV BUY event")
    return cbrs, skhy


def _quote_candidates(
    market_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for session_date in SKHY_SESSIONS:
        old_quotes = [
            event
            for event in market_events
            if event["action"] == "QUOTE"
            and event.get("symbol") == "SKHYV"
            and event.get("session_date") == session_date
        ]
        if len(old_quotes) != 1:
            raise IdentityMigrationError(
                f"expected one SKHYV quote for {session_date}"
            )
        existing_new = [
            event
            for event in market_events
            if event["action"] == "QUOTE"
            and event.get("symbol") == "SKHY"
            and event.get("session_date") == session_date
        ]
        if len(existing_new) > 1:
            raise IdentityMigrationError(
                f"multiple SKHY quotes already exist for {session_date}"
            )
        if existing_new:
            if Decimal(existing_new[0]["close"]) != Decimal(old_quotes[0]["close"]):
                raise IdentityMigrationError(
                    f"conflicting SKHY quote for {session_date}"
                )
            continue
        candidates.append(_market_quote_event(old_quotes[0]))
    return candidates


def _financial_invariants(result: ReplayResult) -> dict[str, Any]:
    realized_values = tuple(
        (
            entry["occurred_at"],
            entry["shares"],
            entry["price"],
            entry["fee"],
            entry["pnl"],
            entry["pnl_pct"],
            entry["cumulative_pnl"],
        )
        for entry in result.realized_pnl_per_trade
    )
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
        "realized_values": realized_values,
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
        raise IdentityMigrationError(
            "identity correction would change portfolio financial invariants"
        )
    before_holdings = _holding_map(before)
    after_holdings = _holding_map(after)
    old_skhy = before_holdings.pop(SKHY_OLD_ID, None)
    new_skhy = after_holdings.pop(SKHY_NEW_ID, None)
    if old_skhy is None or new_skhy is None:
        raise IdentityMigrationError(
            "SKHY correction must replace exactly one open identity"
        )
    if before_holdings != after_holdings:
        raise IdentityMigrationError(
            "identity correction would change an unrelated holding"
        )
    for field in (
        "shares",
        "avg_cost",
        "cost_basis",
        "contract_multiplier",
    ):
        if old_skhy[field] != new_skhy[field]:
            raise IdentityMigrationError(
                f"SKHY correction would change holding field: {field}"
            )
    if (
        new_skhy["symbol"] != "SKHY"
        or new_skhy["instrument_type"] != "EQUITY"
        or new_skhy["quote_symbol"] != "SKHY"
    ):
        raise IdentityMigrationError("corrected SKHY holding metadata is invalid")
    for result in (before, after):
        holdings = _holding_map(result)
        if CBRS_OLD_ID in holdings or CBRS_NEW_ID in holdings:
            raise IdentityMigrationError("CBRS must remain fully closed")


def _assert_already_corrected(result: ReplayResult) -> None:
    holdings = _holding_map(result)
    if CBRS_OLD_ID in holdings or SKHY_OLD_ID in holdings:
        raise IdentityMigrationError("an obsolete holding identity remains active")
    skhy = holdings.get(SKHY_NEW_ID)
    if (
        skhy is None
        or skhy["symbol"] != "SKHY"
        or skhy["instrument_type"] != "EQUITY"
        or skhy["quote_symbol"] != "SKHY"
    ):
        raise IdentityMigrationError("corrected SKHY holding is missing")
    if CBRS_NEW_ID in holdings:
        raise IdentityMigrationError("CBRS must remain fully closed")


def inspect_migration(root: Path) -> MigrationPlan:
    store = LedgerStore(root)
    live_events = store.read("live", repair_tail=False)
    market_events = store.read("market", repair_tail=False)
    if not live_events:
        raise IdentityMigrationError("Live ledger is not initialized")

    cbrs_targets, skhy_targets = _target_events(live_events)
    targets = cbrs_targets + skhy_targets
    corrections = _correction_events(targets)
    existing_by_id = _existing_event_map(live_events + market_events)
    correction_presence = [
        event["event_id"] in existing_by_id
        for event in corrections
    ]
    already_corrected = all(correction_presence)

    correction_ids = {event["event_id"] for event in corrections}
    baseline_live = [
        event
        for event in live_events
        if event["event_id"] not in correction_ids
    ]
    baseline_effective = resolve_effective_events(baseline_live)
    baseline_old = {
        event["event_id"]
        for event in baseline_effective
        if event.get("instrument_id") in {CBRS_OLD_ID, SKHY_OLD_ID}
    }
    target_ids = {event["event_id"] for event in targets}
    if baseline_old != target_ids:
        raise IdentityMigrationError(
            "obsolete identities have unsupported dependent events"
        )

    before = replay_portfolio(baseline_live, portfolio="live")
    proposed_live, correction_pending, correction_duplicates = _simulate(
        live_events,
        corrections,
    )
    after = replay_portfolio(proposed_live, portfolio="live")
    _assert_reclassification_invariants(before, after)
    if already_corrected:
        _assert_already_corrected(after)

    quotes = _quote_candidates(market_events)
    proposed_market, quote_pending, quote_duplicates = _simulate(
        market_events,
        quotes,
    )
    resolve_effective_events(proposed_market)

    return MigrationPlan(
        candidates=corrections + quotes,
        pending=correction_pending + quote_pending,
        duplicates=correction_duplicates + quote_duplicates,
        target_events=len(targets),
        quote_events=len(SKHY_SESSIONS),
        already_corrected=already_corrected,
    )


def _verify_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    live = snapshot["portfolios"]["live"]
    holdings = {
        holding["instrument_id"]: holding
        for holding in live["holdings"]
    }
    for obsolete in (CBRS_OLD_ID, SKHY_OLD_ID):
        if obsolete in holdings:
            raise IdentityMigrationError(
                f"snapshot still has obsolete identity: {obsolete}"
            )
    if CBRS_NEW_ID in holdings:
        raise IdentityMigrationError("CBRS is no longer a closed position")
    skhy = holdings.get(SKHY_NEW_ID)
    if skhy is None:
        raise IdentityMigrationError("snapshot is missing EQUITY:SKHY")
    if (
        skhy["symbol"] != "SKHY"
        or skhy["instrument_type"] != "EQUITY"
        or skhy["quote_symbol"] != "SKHY"
        or skhy["quote_status"] != "OK"
        or Decimal(skhy["shares"]) != EXPECTED_SKHY_SHARES
        or Decimal(skhy["avg_cost"]) != EXPECTED_SKHY_AVG_COST
        or Decimal(skhy["current_price"]) != EXPECTED_SKHY_LATEST_CLOSE
    ):
        raise IdentityMigrationError("snapshot SKHY holding is invalid")

    trades = live["recent_trades"]
    if any(
        trade.get("instrument_id") in {CBRS_OLD_ID, SKHY_OLD_ID}
        for trade in trades
    ):
        raise IdentityMigrationError(
            "snapshot trade history retains an obsolete identity"
        )
    cbrs_count = sum(
        trade.get("instrument_id") == CBRS_NEW_ID
        for trade in trades
    )
    skhy_count = sum(
        trade.get("instrument_id") == SKHY_NEW_ID
        for trade in trades
    )
    if cbrs_count != 5 or skhy_count != 1:
        raise IdentityMigrationError(
            "snapshot corrected trade counts are invalid"
        )

    metrics = live["metrics"]
    if (
        metrics["data_status"] != "OK"
        or metrics["performance_effective_date"] != EXPECTED_EFFECTIVE_DATE
        or metrics["performance_scope"] != "LATEST_COMPLETE_SEGMENT"
    ):
        raise IdentityMigrationError(
            "Live performance metadata changed unexpectedly"
        )
    return {
        "cbrs_trade_count": cbrs_count,
        "skhy_trade_count": skhy_count,
        "skhy_instrument_id": skhy["instrument_id"],
        "skhy_quote_status": skhy["quote_status"],
        "skhy_current_price": skhy["current_price"],
        "performance_effective_date": metrics["performance_effective_date"],
        "performance_scope": metrics["performance_scope"],
    }


def execute(*, root: Path, apply: bool) -> dict[str, Any]:
    plan = inspect_migration(root)
    response: dict[str, Any] = {
        "status": "valid" if not apply else "ready",
        "migration": "listed-instrument-identities-2026",
        "target_events": plan.target_events,
        "quote_events": plan.quote_events,
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
            "requested_by": "listed-instrument-identity-migration",
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
    command = argparse.ArgumentParser(
        prog="repair-instrument-identities"
    )
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
        IdentityMigrationError,
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
