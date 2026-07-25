"""Exact decimal parsing and JSON formatting helpers."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

SHARES_QUANT = Decimal("0.00000001")
PRICE_QUANT = Decimal("0.000001")
MONEY_QUANT = Decimal("0.01")
PERCENT_QUANT = Decimal("0.00000001")
ZERO = Decimal("0")


def as_decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a decimal number")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be a decimal number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def quantize(value: Any, quantum: Decimal, *, field: str) -> Decimal:
    return as_decimal(value, field=field).quantize(quantum, rounding=ROUND_HALF_UP)


def _bounded_decimal(value: Any, *, field: str, max_places: int) -> Decimal:
    parsed = as_decimal(value, field=field)
    places = max(0, -parsed.as_tuple().exponent)
    if places > max_places:
        raise ValueError(f"{field} supports at most {max_places} decimal places")
    return parsed


def money(value: Any, *, field: str = "amount") -> Decimal:
    return as_decimal(value, field=field)


def price(value: Any, *, field: str = "price") -> Decimal:
    return as_decimal(value, field=field)


def shares(value: Any, *, field: str = "shares") -> Decimal:
    return as_decimal(value, field=field)


def input_price(value: Any, *, field: str = "price") -> Decimal:
    return _bounded_decimal(value, field=field, max_places=6)


def input_shares(value: Any, *, field: str = "shares") -> Decimal:
    return _bounded_decimal(value, field=field, max_places=8)


def percent(value: Decimal) -> Decimal:
    return value.quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


def amount_for(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return quantity * unit_price


def decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return f"{normalized:.0f}"
    return format(normalized, "f")


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
