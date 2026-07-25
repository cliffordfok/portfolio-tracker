"""Validation for append-only portfolio and market events."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from .decimal_utils import input_price, input_shares, money
from .errors import ValidationError

PORTFOLIOS = {"paper", "live", "market"}
ECONOMIC_ACTIONS = {"PORTFOLIO_OPEN", "BUY", "SELL", "CASH_FLOW"}
CORRECTION_ACTIONS = {"AMEND", "VOID"}
MARKET_ACTIONS = {"QUOTE", "BENCHMARK_CLOSE"}
ACTIONS = ECONOMIC_ACTIONS | CORRECTION_ACTIONS | MARKET_ACTIONS
CORRECTABLE_ACTIONS = {"BUY", "SELL", "CASH_FLOW"}
MUTABLE_FIELDS = {"note", "fee", "reason", "strategy"}
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValidationError(f"{field} must be an ISO8601 UTC string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} must be an ISO8601 UTC string") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _require(event: Mapping[str, Any], fields: set[str]) -> None:
    missing = sorted(field for field in fields if field not in event)
    if missing:
        raise ValidationError(f"missing required fields: {', '.join(missing)}")


def _validate_symbol(value: Any) -> None:
    if not isinstance(value, str) or not SYMBOL_RE.fullmatch(value):
        raise ValidationError("symbol must match ^[A-Z]{1,5}(\\.[A-Z])?$")


def _validate_string(event: Mapping[str, Any], field: str) -> None:
    if field in event and event[field] is not None and not isinstance(event[field], str):
        raise ValidationError(f"{field} must be a string")


def validate_event(
    event: Mapping[str, Any],
    *,
    now: datetime | None = None,
    allow_future: bool = False,
) -> None:
    """Validate one event without mutating it."""

    _require(
        event,
        {"event_id", "portfolio", "occurred_at", "created_at", "source", "action"},
    )
    event_id = event["event_id"]
    portfolio = event["portfolio"]
    action = event["action"]

    if portfolio not in PORTFOLIOS:
        raise ValidationError("portfolio must be paper, live, or market")
    if action not in ACTIONS:
        raise ValidationError(f"unsupported action: {action}")
    if not isinstance(event_id, str) or not event_id.startswith(f"{portfolio}-"):
        raise ValidationError(f"event_id must start with '{portfolio}-'")
    if len(event_id) > 128:
        raise ValidationError("event_id must be at most 128 characters")
    forbidden_derived = sorted({"pnl", "pnl_pct"} & set(event))
    if forbidden_derived:
        raise ValidationError(
            f"derived fields are forbidden in master events: "
            f"{', '.join(forbidden_derived)}"
        )

    occurred_at = parse_timestamp(event["occurred_at"], field="occurred_at")
    parse_timestamp(event["created_at"], field="created_at")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if not allow_future and occurred_at > current + timedelta(minutes=5):
        raise ValidationError("occurred_at cannot be in the future")

    if "ledger_seq" in event:
        seq = event["ledger_seq"]
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
            raise ValidationError("ledger_seq must be a positive integer")

    for optional in ("note", "reason", "strategy", "source"):
        _validate_string(event, optional)
    if not event["source"]:
        raise ValidationError("source must be a non-empty string")

    if portfolio == "market" and action not in MARKET_ACTIONS:
        raise ValidationError("market ledger only accepts QUOTE/BENCHMARK_CLOSE")
    if portfolio != "market" and action in MARKET_ACTIONS:
        raise ValidationError("QUOTE/BENCHMARK_CLOSE must use market portfolio")

    if action == "PORTFOLIO_OPEN":
        _require(event, {"initial_cash", "currency"})
        if money(event["initial_cash"], field="initial_cash") < 0:
            raise ValidationError("initial_cash must be non-negative")
        if event["currency"] != "USD":
            raise ValidationError("currency must be USD")
    elif action in {"BUY", "SELL"}:
        _require(event, {"symbol", "shares", "price"})
        _validate_symbol(event["symbol"])
        if input_shares(event["shares"]) <= 0:
            raise ValidationError("shares must be greater than zero")
        if input_price(event["price"]) <= 0:
            raise ValidationError("price must be greater than zero")
        if money(event.get("fee", 0), field="fee") < 0:
            raise ValidationError("fee must be non-negative")
    elif action == "CASH_FLOW":
        _require(event, {"amount"})
        money(event["amount"])
    elif action == "AMEND":
        _require(event, {"amend_target", "changes"})
        if not isinstance(event["amend_target"], str):
            raise ValidationError("amend_target must be an event_id")
        changes = event["changes"]
        if not isinstance(changes, dict) or not changes:
            raise ValidationError("changes must be a non-empty object")
        invalid = sorted(set(changes) - MUTABLE_FIELDS)
        if invalid:
            raise ValidationError(f"immutable fields cannot be amended: {', '.join(invalid)}")
        if "fee" in changes and money(changes["fee"], field="changes.fee") < 0:
            raise ValidationError("changes.fee must be non-negative")
        for field in set(changes) - {"fee"}:
            if changes[field] is not None and not isinstance(changes[field], str):
                raise ValidationError(f"changes.{field} must be a string or null")
    elif action == "VOID":
        _require(event, {"void_target"})
        if not isinstance(event["void_target"], str):
            raise ValidationError("void_target must be an event_id")
    elif action in MARKET_ACTIONS:
        _require(event, {"symbol", "close", "session_date"})
        _validate_symbol(event["symbol"])
        if input_price(event["close"], field="close") <= 0:
            raise ValidationError("close must be greater than zero")
        if not isinstance(event["session_date"], str) or not DATE_RE.fullmatch(
            event["session_date"]
        ):
            raise ValidationError("session_date must use YYYY-MM-DD")
