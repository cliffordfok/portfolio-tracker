"""Remove legacy underlying quote aliases from Live option trades.

The migration is append-only: each affected trade is replaced with the same
economic facts (without ``quote_symbol``), then the original event is voided.
This prevents an option contract from being valued with its underlying equity
price while preserving the complete audit trail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import dataclass
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
from portfolio_tracker.schemas import (
    ACTION_FIELDS,
    normalize_event,
    validate_event,
    validate_intake_instrument_identity,
)
from portfolio_tracker.snapshot import build_snapshot_if_needed

MIGRATION_CREATED_AT = "2026-07-29T00:00:00Z"
MIGRATION_PREFIX = "live-option-identity-"


class OptionIdentityMigrationError(ValueError):
    """Raised when the legacy option correction is unsafe."""


@dataclass
class MigrationPlan:
    candidates: list[dict[str, Any]]
    pending: int
    duplicates: int
    target_events: int
    already_corrected: bool


def _without_sequence(event: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(event)
    value.pop("ledger_seq", None)
    return normalize_event(value)


def _event_suffix(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]


def _replacement_id(target: dict[str, Any]) -> str:
    return f"{MIGRATION_PREFIX}replacement-{_event_suffix(target['event_id'])}"


def _void_id(target: dict[str, Any]) -> str:
    return f"{MIGRATION_PREFIX}void-{_event_suffix(target['event_id'])}"


def _replacement_event(target: dict[str, Any]) -> dict[str, Any]:
    replacement = {
        "event_id": _replacement_id(target),
        "portfolio": "live",
        "occurred_at": target["occurred_at"],
        "created_at": MIGRATION_CREATED_AT,
        "source": "manual-import",
        "action": target["action"],
    }
    for field in ACTION_FIELDS[target["action"]]:
        if field in target and field != "quote_symbol":
            replacement[field] = deepcopy(target[field])
    return replacement


def _void_event(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": _void_id(target),
        "portfolio": "live",
        "occurred_at": MIGRATION_CREATED_AT,
        "created_at": MIGRATION_CREATED_AT,
        "source": "manual-import",
        "action": "VOID",
        "void_target": target["event_id"],
        "void_reason": (
            "Remove a legacy underlying quote alias from an option contract"
        ),
    }


def _correction_events(
    targets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for target in targets:
        candidates.extend([_replacement_event(target), _void_event(target)])
    return candidates


def _existing_event_map(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = event["event_id"]
        if event_id in result:
            raise OptionIdentityMigrationError(
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
        if candidate["action"] in {"BUY", "SELL"}:
            validate_intake_instrument_identity(candidate)
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


def _target_events(live_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    effective_ids = {
        event["event_id"]
        for event in resolve_effective_events(live_events)
    }
    existing_ids = {event["event_id"] for event in live_events}
    targets: list[dict[str, Any]] = []
    for event in live_events:
        if (
            event["action"] not in {"BUY", "SELL"}
            or event.get("instrument_type") != "OPTION"
            or event.get("quote_symbol") is None
            or event["event_id"].startswith(MIGRATION_PREFIX)
        ):
            continue
        migration_started = (
            _replacement_id(event) in existing_ids
            or _void_id(event) in existing_ids
        )
        if event["event_id"] in effective_ids or migration_started:
            targets.append(event)
    return targets


def _financial_invariants(result: ReplayResult) -> dict[str, Any]:
    holding_economics = sorted(
        (
            holding["instrument_id"],
            holding["symbol"],
            holding["instrument_type"],
            holding["shares"],
            holding["avg_cost"],
            holding["cost_basis"],
            holding["contract_multiplier"],
        )
        for holding in result.holdings
    )
    realized_values = tuple(
        (
            entry["occurred_at"],
            entry["instrument_id"],
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
        "holdings": holding_economics,
        "closed_episode_count": len(result.closed_episodes),
    }


def inspect_migration(root: Path) -> MigrationPlan:
    live_events = LedgerStore(root).read("live", repair_tail=False)
    if not live_events:
        raise OptionIdentityMigrationError("Live ledger is not initialized")

    targets = _target_events(live_events)
    candidates = _correction_events(targets)
    candidate_ids = {event["event_id"] for event in candidates}
    baseline = [
        event
        for event in live_events
        if event["event_id"] not in candidate_ids
    ]
    before = replay_portfolio(baseline, portfolio="live")
    proposed, pending, duplicates = _simulate(live_events, candidates)
    after = replay_portfolio(proposed, portfolio="live")
    if _financial_invariants(before) != _financial_invariants(after):
        raise OptionIdentityMigrationError(
            "option identity correction would change financial invariants"
        )

    effective_options = [
        event
        for event in resolve_effective_events(proposed)
        if event.get("instrument_type") == "OPTION"
    ]
    if any(event.get("quote_symbol") is not None for event in effective_options):
        raise OptionIdentityMigrationError(
            "an effective option event still has quote_symbol"
        )

    return MigrationPlan(
        candidates=candidates,
        pending=pending,
        duplicates=duplicates,
        target_events=len(targets),
        already_corrected=bool(targets) and pending == 0,
    )


def execute(*, root: Path, apply: bool) -> dict[str, Any]:
    plan = inspect_migration(root)
    response: dict[str, Any] = {
        "status": "valid" if not apply else "ready",
        "migration": "legacy-option-quote-identities",
        "target_events": plan.target_events,
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
            }
        )
        return response

    backup = backup_ledgers(root)
    plan = inspect_migration(root)
    if plan.pending == 0:
        response.update({"status": "current", "backup_id": backup["backup_id"]})
        return response

    batch = LedgerStore(root).append_many(plan.candidates)
    snapshot, rebuilt = build_snapshot_if_needed(root)
    atomic_write_json(
        root / "state" / "publish.pending",
        {
            "revision": snapshot["revision"],
            "requested_by": "legacy-option-identity-migration",
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
        }
    )
    return response


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="repair-legacy-option-identities"
    )
    command.add_argument("--root", required=True)
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check-only", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(root=Path(args.root).resolve(), apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        ConflictError,
        PortfolioError,
        OptionIdentityMigrationError,
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
