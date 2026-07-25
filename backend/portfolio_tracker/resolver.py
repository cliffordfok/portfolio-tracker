"""Two-pass correction resolver for immutable JSONL ledgers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from .errors import BusinessInvariantError, ConflictError
from .schemas import CORRECTABLE_ACTIONS, parse_timestamp, validate_event


def _sort_key(event: dict[str, Any]) -> tuple[Any, int, Any]:
    return (
        parse_timestamp(event["occurred_at"], field="occurred_at"),
        int(event["ledger_seq"]),
        parse_timestamp(event["created_at"], field="created_at"),
    )


def resolve_effective_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve AMEND/VOID metadata, then return sorted economic events.

    AMEND targets BUY, SELL, or CASH_FLOW. VOID targets an economic event or
    an AMEND; when it targets an AMEND, the underlying economic event is
    voided. Corrections always target a prior event in the same append-only
    ledger. AMEND is per-field last-write-wins and VOID overrides all
    amendments.
    """

    raw = [deepcopy(event) for event in events]
    if not raw:
        return []

    seen: dict[str, dict[str, Any]] = {}
    voided: set[str] = set()
    amendments: dict[str, dict[str, Any]] = {}
    previous_seq = 0
    portfolio = raw[0].get("portfolio")

    for event in raw:
        validate_event(event, allow_future=True)
        if event["portfolio"] != portfolio:
            raise BusinessInvariantError("one replay cannot mix portfolio ledgers")
        if "ledger_seq" not in event:
            raise BusinessInvariantError("stored events require ledger_seq")
        seq = int(event["ledger_seq"])
        if seq <= previous_seq:
            raise BusinessInvariantError("ledger_seq must increase in append order")
        previous_seq = seq

        event_id = event["event_id"]
        if event_id in seen:
            raise ConflictError(f"duplicate event_id in ledger: {event_id}")

        action = event["action"]
        if action == "AMEND":
            target_id = event["amend_target"]
            target = seen.get(target_id)
            if target is None:
                raise BusinessInvariantError(f"AMEND target not found: {target_id}")
            if target["portfolio"] != event["portfolio"]:
                raise BusinessInvariantError("cross-portfolio AMEND is forbidden")
            if target["action"] not in CORRECTABLE_ACTIONS:
                raise BusinessInvariantError("AMEND may target BUY/SELL/CASH_FLOW only")
            if target_id in voided:
                raise BusinessInvariantError(f"AMEND after VOID: {target_id}")
            amendments.setdefault(target_id, {}).update(deepcopy(event["changes"]))

        elif action == "VOID":
            target_id = event["void_target"]
            target = seen.get(target_id)
            if target is None:
                raise BusinessInvariantError(f"VOID target not found: {target_id}")
            if target["portfolio"] != event["portfolio"]:
                raise BusinessInvariantError("cross-portfolio VOID is forbidden")
            if target["action"] == "VOID":
                raise BusinessInvariantError("VOID may not target another VOID")
            if target["action"] == "AMEND":
                economic_target_id = target["amend_target"]
            elif target["action"] in CORRECTABLE_ACTIONS:
                economic_target_id = target_id
            else:
                raise BusinessInvariantError(
                    "VOID may target BUY/SELL/CASH_FLOW/AMEND only"
                )
            if economic_target_id in voided:
                raise BusinessInvariantError(f"duplicate VOID: {economic_target_id}")
            voided.add(economic_target_id)

        seen[event_id] = event

    effective: list[dict[str, Any]] = []
    for event in raw:
        if event["action"] in {"AMEND", "VOID"}:
            continue
        if event["event_id"] in voided:
            continue
        resolved = deepcopy(event)
        resolved.update(amendments.get(event["event_id"], {}))
        effective.append(resolved)

    effective.sort(key=_sort_key)
    return effective
