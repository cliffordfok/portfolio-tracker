"""Validate and atomically import an approved Live portfolio staging plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from portfolio_tracker.errors import ConflictError, PortfolioError
from portfolio_tracker.ledger import LedgerStore, atomic_write_json
from portfolio_tracker.replay import ReplayResult, replay_portfolio
from portfolio_tracker.schemas import (
    normalize_event,
    validate_event,
    validate_intake_instrument_identity,
)
from portfolio_tracker.snapshot import build_snapshot_if_needed


class LiveImportError(ValueError):
    """Raised when an import plan is unsafe or internally inconsistent."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveImportError("staging plan must be valid UTF-8 JSON") from exc
    if not isinstance(plan, dict):
        raise LiveImportError("staging plan must be an object")
    required = {"plan_version", "source", "events", "expected"}
    missing = sorted(required - set(plan))
    if missing:
        raise LiveImportError(f"staging plan missing: {', '.join(missing)}")
    unknown = sorted(set(plan) - required)
    if unknown:
        raise LiveImportError(f"staging plan unknown fields: {', '.join(unknown)}")
    if plan["plan_version"] != 1:
        raise LiveImportError("staging plan_version must be 1")
    source = plan["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"filename", "sha256"}
        or not isinstance(source["filename"], str)
        or not isinstance(source["sha256"], str)
        or len(source["sha256"]) != 64
    ):
        raise LiveImportError("staging source metadata is invalid")
    events = plan["events"]
    if not isinstance(events, list) or not events:
        raise LiveImportError("staging events must be a non-empty array")
    if not isinstance(plan["expected"], dict):
        raise LiveImportError("staging expected must be an object")
    return plan


def _without_sequence(event: dict[str, Any]) -> dict[str, Any]:
    value = deepcopy(event)
    value.pop("ledger_seq", None)
    return normalize_event(value)


def _events_for_replay(
    candidates: list[dict[str, Any]],
    existing: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    existing_by_id = {event["event_id"]: event for event in existing}
    if len(existing_by_id) != len(existing):
        raise LiveImportError("existing Live ledger has duplicate event IDs")
    next_sequence = max(
        (int(event["ledger_seq"]) for event in existing),
        default=0,
    ) + 1
    proposed = [deepcopy(event) for event in existing]
    duplicates = 0
    for candidate in candidates:
        event_id = candidate["event_id"]
        stored = existing_by_id.get(event_id)
        if stored is not None:
            if _without_sequence(stored) != normalize_event(candidate):
                raise ConflictError(
                    "event_id already exists with different payload: "
                    f"{event_id}"
                )
            duplicates += 1
            continue
        item = normalize_event(candidate)
        item["ledger_seq"] = next_sequence
        next_sequence += 1
        proposed.append(item)
    return proposed, duplicates


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return (
        f"{normalized:.0f}"
        if normalized == normalized.to_integral()
        else format(normalized, "f")
    )


def result_summary(result: ReplayResult) -> dict[str, Any]:
    cents = Decimal("0.01")
    return {
        "ending_cash": _decimal_text(
            result.cash.quantize(cents, rounding=ROUND_HALF_UP)
        ),
        "income_expense_total": _decimal_text(
            result.income_expense_total.quantize(
                cents,
                rounding=ROUND_HALF_UP,
            )
        ),
        "realized_pnl": _decimal_text(
            result.realized_pnl_total.quantize(cents, rounding=ROUND_HALF_UP)
        ),
        "holdings": {
            holding["instrument_id"]: _decimal_text(holding["shares"])
            for holding in result.holdings
        },
    }


def validate_plan(
    plan: dict[str, Any],
    *,
    existing: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], ReplayResult, int]:
    candidates = plan["events"]
    ids: set[str] = set()
    previous_at: str | None = None
    for index, event in enumerate(candidates, start=1):
        if not isinstance(event, dict):
            raise LiveImportError(f"event {index} must be an object")
        if "ledger_seq" in event:
            raise LiveImportError(f"event {index} must not contain ledger_seq")
        validate_event(event, allow_future=True)
        validate_intake_instrument_identity(event)
        if event["portfolio"] != "live":
            raise LiveImportError(f"event {index} must target live")
        if event["event_id"] in ids:
            raise LiveImportError(
                f"duplicate staging event_id: {event['event_id']}"
            )
        ids.add(event["event_id"])
        if previous_at is not None and event["occurred_at"] < previous_at:
            raise LiveImportError("staging events must be sorted by occurred_at")
        previous_at = event["occurred_at"]
    if candidates[0]["action"] != "PORTFOLIO_OPEN":
        raise LiveImportError("first staging event must be PORTFOLIO_OPEN")

    proposed, duplicates = _events_for_replay(candidates, existing or [])
    result = replay_portfolio(proposed, portfolio="live")
    actual = result_summary(result)
    if actual != plan["expected"]:
        raise LiveImportError(
            "staging business invariants do not match expected summary"
        )
    return candidates, result, duplicates


def execute(
    *,
    root: Path,
    plan_path: Path,
    source_workbook: Path | None,
    apply: bool,
) -> dict[str, Any]:
    plan = load_plan(plan_path)
    if source_workbook is not None:
        if source_workbook.name != plan["source"]["filename"]:
            raise LiveImportError("source workbook filename mismatch")
        if file_sha256(source_workbook) != plan["source"]["sha256"]:
            raise LiveImportError("source workbook SHA-256 mismatch")

    store = LedgerStore(root)
    existing = store.read("live", repair_tail=False)
    candidates, result, duplicates = validate_plan(plan, existing=existing)
    pending = len(candidates) - duplicates
    response = {
        "status": "valid" if not apply else "ready",
        "events": len(candidates),
        "existing_duplicates": duplicates,
        "pending": pending,
        **result_summary(result),
    }
    if not apply:
        return response

    batch = store.append_many(candidates)
    snapshot, rebuilt = build_snapshot_if_needed(root)
    atomic_write_json(
        root / "state" / "publish.pending",
        {
            "revision": snapshot["revision"],
            "requested_by": "live-staging-import",
        },
    )
    response.update(
        {
            "status": "imported" if batch["appended"] else "duplicate",
            "appended": batch["appended"],
            "duplicates": batch["duplicates"],
            "snapshot_revision": snapshot["revision"],
            "snapshot_status": "rebuilt" if rebuilt else "current",
        }
    )
    return response


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="import-live-staging")
    command.add_argument("--root", required=True)
    command.add_argument("--plan", required=True)
    command.add_argument("--source-workbook")
    mode = command.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check-only", action="store_true")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = execute(
            root=Path(args.root).resolve(),
            plan_path=Path(args.plan).resolve(),
            source_workbook=(
                Path(args.source_workbook).resolve()
                if args.source_workbook
                else None
            ),
            apply=args.apply,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (
        LiveImportError,
        PortfolioError,
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
