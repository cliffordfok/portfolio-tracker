"""Validation for append-only portfolio and market events."""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from typing import Any, Mapping

from .decimal_utils import input_price, input_shares, money
from .errors import ValidationError

PORTFOLIOS = {"paper", "live", "market"}
ECONOMIC_ACTIONS = {
    "PORTFOLIO_OPEN",
    "BUY",
    "SELL",
    "CASH_FLOW",
    "INCOME_EXPENSE",
    "SPLIT",
}
CORRECTION_ACTIONS = {"AMEND", "VOID"}
MARKET_ACTIONS = {"QUOTE", "BENCHMARK_CLOSE"}
ACTIONS = ECONOMIC_ACTIONS | CORRECTION_ACTIONS | MARKET_ACTIONS
CORRECTABLE_ACTIONS = {
    "BUY",
    "SELL",
    "CASH_FLOW",
    "INCOME_EXPENSE",
    "SPLIT",
}
MUTABLE_FIELDS = {"note", "fee", "reason", "strategy"}
SOURCES = {
    "bootstrap",
    "telegram",
    "swing-trader",
    "manual-import",
    "cron-benchmark",
    "cron-quote",
    "manual-quote",
}
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
INSTRUMENT_ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9:._/-]{0,95}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INSTRUMENT_TYPES = {"EQUITY", "ETF", "OPTION", "PRIVATE"}
INCOME_TYPES = {"DIVIDEND", "INTEREST", "FEE", "CASH_IN_LIEU", "OTHER"}
INSTRUMENT_FIELDS = {
    "instrument_id",
    "instrument_type",
    "instrument_name",
    "quote_symbol",
    "contract_multiplier",
}
BASE_FIELDS = {
    "event_id",
    "portfolio",
    "occurred_at",
    "created_at",
    "source",
    "action",
    "ledger_seq",
}
ACTION_FIELDS = {
    "PORTFOLIO_OPEN": {"initial_cash", "currency"},
    "BUY": {
        "symbol",
        "shares",
        "price",
        "fee",
        "note",
        "strategy",
        "reason",
    }
    | INSTRUMENT_FIELDS,
    "SELL": {
        "symbol",
        "shares",
        "price",
        "fee",
        "note",
        "strategy",
        "reason",
    }
    | INSTRUMENT_FIELDS,
    "CASH_FLOW": {"symbol", "amount", "note"},
    "INCOME_EXPENSE": {
        "symbol",
        "instrument_id",
        "instrument_name",
        "amount",
        "gross_amount",
        "withholding_tax",
        "income_type",
        "note",
    },
    "SPLIT": {
        "symbol",
        "instrument_id",
        "instrument_type",
        "instrument_name",
        "quote_symbol",
        "numerator",
        "denominator",
        "note",
    },
    "AMEND": {"amend_target", "changes", "amend_reason"},
    "VOID": {"void_target", "void_reason"},
    "QUOTE": {"symbol", "instrument_id", "close", "session_date"},
    "BENCHMARK_CLOSE": {"symbol", "close", "session_date"},
}


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


def _validate_instrument_id(value: Any, *, required: bool = False) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or not INSTRUMENT_ID_RE.fullmatch(value):
        raise ValidationError(
            "instrument_id must use 1-96 uppercase letters, numbers, or :._/-"
        )


def _validate_instrument_fields(event: Mapping[str, Any]) -> None:
    _validate_instrument_id(event.get("instrument_id"))
    instrument_type = event.get("instrument_type")
    if (
        instrument_type is not None
        and instrument_type not in INSTRUMENT_TYPES
    ):
        raise ValidationError(
            "instrument_type must be EQUITY, ETF, OPTION, or PRIVATE"
        )
    if instrument_type in {"OPTION", "PRIVATE"} and not event.get(
        "instrument_id"
    ):
        raise ValidationError(
            "OPTION and PRIVATE trades require a stable instrument_id"
        )
    _validate_string(event, "instrument_name")
    quote_symbol = event.get("quote_symbol")
    if quote_symbol is not None:
        _validate_symbol(quote_symbol)
    if "contract_multiplier" in event:
        multiplier = input_shares(
            event["contract_multiplier"],
            field="contract_multiplier",
        )
        if multiplier <= 0:
            raise ValidationError("contract_multiplier must be greater than zero")


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
    forbidden_derived = sorted({"pnl", "pnl_pct"} & set(event))
    if forbidden_derived:
        raise ValidationError(
            f"derived fields are forbidden in master events: "
            f"{', '.join(forbidden_derived)}"
        )
    unknown_fields = sorted(set(event) - BASE_FIELDS - ACTION_FIELDS[action])
    if unknown_fields:
        raise ValidationError(
            f"unknown fields for {action}: {', '.join(unknown_fields)}"
        )
    if not isinstance(event_id, str) or not event_id.startswith(f"{portfolio}-"):
        raise ValidationError(f"event_id must start with '{portfolio}-'")
    if len(event_id) > 128:
        raise ValidationError("event_id must be at most 128 characters")

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
    if event["source"] not in SOURCES:
        raise ValidationError(
            f"source must be one of: {', '.join(sorted(SOURCES))}"
        )
    required_source = {
        "PORTFOLIO_OPEN": "bootstrap",
        "BENCHMARK_CLOSE": "cron-benchmark",
    }.get(action)
    if required_source is not None and event["source"] != required_source:
        raise ValidationError(f"{action} source must be {required_source}")
    if action == "QUOTE" and event["source"] not in {
        "cron-quote",
        "manual-quote",
    }:
        raise ValidationError("QUOTE source must be cron-quote or manual-quote")

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
        _require(event, {"symbol", "shares", "price", "fee"})
        _validate_symbol(event["symbol"])
        if input_shares(event["shares"]) <= 0:
            raise ValidationError("shares must be greater than zero")
        if input_price(event["price"]) <= 0:
            raise ValidationError("price must be greater than zero")
        if money(event.get("fee", 0), field="fee") < 0:
            raise ValidationError("fee must be non-negative")
        _validate_instrument_fields(event)
    elif action == "CASH_FLOW":
        _require(event, {"amount", "symbol"})
        if event["symbol"] != "USD":
            raise ValidationError("CASH_FLOW symbol must be USD")
        money(event["amount"])
    elif action == "INCOME_EXPENSE":
        _require(
            event,
            {"symbol", "amount", "withholding_tax", "income_type"},
        )
        _validate_symbol(event["symbol"])
        _validate_instrument_id(event.get("instrument_id"))
        _validate_string(event, "instrument_name")
        amount = money(event["amount"])
        withholding = money(
            event["withholding_tax"],
            field="withholding_tax",
        )
        if withholding < 0:
            raise ValidationError("withholding_tax must be non-negative")
        income_type = event["income_type"]
        if income_type not in INCOME_TYPES:
            raise ValidationError(
                "income_type must be DIVIDEND, INTEREST, FEE, "
                "CASH_IN_LIEU, or OTHER"
            )
        if "gross_amount" in event:
            gross = money(event["gross_amount"], field="gross_amount")
            if money(gross - withholding) != amount:
                raise ValidationError(
                    "amount must equal gross_amount minus withholding_tax"
                )
    elif action == "SPLIT":
        _require(event, {"symbol", "instrument_id", "numerator", "denominator"})
        _validate_symbol(event["symbol"])
        _validate_instrument_id(event["instrument_id"], required=True)
        _validate_instrument_fields(event)
        numerator = input_shares(event["numerator"], field="numerator")
        denominator = input_shares(event["denominator"], field="denominator")
        if numerator <= 0 or denominator <= 0:
            raise ValidationError("split ratio must be greater than zero")
        if numerator == denominator:
            raise ValidationError("split ratio must change the position")
    elif action == "AMEND":
        _require(event, {"amend_target", "changes", "amend_reason"})
        if not isinstance(event["amend_target"], str):
            raise ValidationError("amend_target must be an event_id")
        if (
            not isinstance(event["amend_reason"], str)
            or not event["amend_reason"].strip()
        ):
            raise ValidationError("amend_reason must be a non-empty string")
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
        _require(event, {"void_target", "void_reason"})
        if not isinstance(event["void_target"], str):
            raise ValidationError("void_target must be an event_id")
        if (
            not isinstance(event["void_reason"], str)
            or not event["void_reason"].strip()
        ):
            raise ValidationError("void_reason must be a non-empty string")
    elif action in MARKET_ACTIONS:
        _require(event, {"symbol", "close", "session_date"})
        _validate_symbol(event["symbol"])
        _validate_instrument_id(event.get("instrument_id"))
        if action == "BENCHMARK_CLOSE" and event["symbol"] != "SPY":
            raise ValidationError("BENCHMARK_CLOSE symbol must be SPY")
        if input_price(event["close"], field="close") <= 0:
            raise ValidationError("close must be greater than zero")
        if not isinstance(event["session_date"], str) or not DATE_RE.fullmatch(
            event["session_date"]
        ):
            raise ValidationError("session_date must use YYYY-MM-DD")
        try:
            date.fromisoformat(event["session_date"])
        except ValueError as exc:
            raise ValidationError("session_date must be a real calendar date") from exc


def _decimal_string(value: Any, *, field: str) -> str:
    parsed = money(value, field=field)
    return format(parsed, "f")


def normalize_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe event with every Decimal master fact as a string."""

    normalized = deepcopy(dict(event))
    action = normalized["action"]
    if action == "PORTFOLIO_OPEN":
        normalized["initial_cash"] = _decimal_string(
            normalized["initial_cash"],
            field="initial_cash",
        )
    elif action in {"BUY", "SELL"}:
        normalized["shares"] = format(
            input_shares(normalized["shares"]),
            "f",
        )
        normalized["price"] = format(
            input_price(normalized["price"]),
            "f",
        )
        normalized["fee"] = _decimal_string(
            normalized["fee"],
            field="fee",
        )
        if "contract_multiplier" in normalized:
            normalized["contract_multiplier"] = format(
                input_shares(
                    normalized["contract_multiplier"],
                    field="contract_multiplier",
                ),
                "f",
            )
    elif action == "CASH_FLOW":
        normalized["amount"] = _decimal_string(
            normalized["amount"],
            field="amount",
        )
    elif action == "INCOME_EXPENSE":
        normalized["amount"] = _decimal_string(
            normalized["amount"],
            field="amount",
        )
        normalized["withholding_tax"] = _decimal_string(
            normalized["withholding_tax"],
            field="withholding_tax",
        )
        if "gross_amount" in normalized:
            normalized["gross_amount"] = _decimal_string(
                normalized["gross_amount"],
                field="gross_amount",
            )
    elif action == "SPLIT":
        normalized["numerator"] = format(
            input_shares(normalized["numerator"], field="numerator"),
            "f",
        )
        normalized["denominator"] = format(
            input_shares(normalized["denominator"], field="denominator"),
            "f",
        )
    elif action == "AMEND" and "fee" in normalized["changes"]:
        normalized["changes"]["fee"] = _decimal_string(
            normalized["changes"]["fee"],
            field="changes.fee",
        )
    elif action in MARKET_ACTIONS:
        normalized["close"] = format(
            input_price(normalized["close"], field="close"),
            "f",
        )
    return normalized
