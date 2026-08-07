"""Build the public, derived dashboard snapshot from private master ledgers."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .decimal_utils import ZERO, amount_for, json_safe, money, percent, price, shares
from .errors import BusinessInvariantError, ValidationError
from .ledger import FileLock, LedgerStore, atomic_write_json, durable_unlink
from .market_time import (
    is_nyse_session,
    latest_completed_nyse_session,
    nyse_sessions,
)
from .replay import ReplayResult, replay_portfolio
from .schemas import (
    INSTRUMENT_ID_RE,
    INSTRUMENT_TYPES,
    SYMBOL_RE,
    parse_timestamp,
)

try:
    NEW_YORK: ZoneInfo | None = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    NEW_YORK = None
MARKET_CLOSE = time(16, 0)


def _snapshot_decimal_or_none(value: Any, *, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a Decimal string or null")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError(
            f"{label} must be a Decimal string or null"
        ) from exc
    if not parsed.is_finite():
        raise ValidationError(f"{label} must be finite")


def _snapshot_date(value: Any, *, label: str) -> None:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{label} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValidationError(f"{label} must use YYYY-MM-DD")


def _snapshot_timestamp(
    value: Any,
    *,
    label: str,
    nullable: bool = True,
) -> None:
    if value is None and nullable:
        return
    try:
        parse_timestamp(value, field=label)
    except (ValidationError, ValueError) as exc:
        raise ValidationError(
            f"{label} must be a UTC timestamp or null"
        ) from exc


def validate_snapshot(snapshot: Any) -> None:
    """Validate the complete intrinsic schema of one public snapshot."""

    if not isinstance(snapshot, dict):
        raise ValidationError("portfolio snapshot must be a JSON object")
    if snapshot.get("schema_version") != 4:
        raise ValidationError("portfolio snapshot schema_version must be 4")
    revision = snapshot.get("revision")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ValidationError(
            "portfolio snapshot revision must be a non-negative integer"
        )
    if snapshot.get("currency") != "USD":
        raise ValidationError("portfolio snapshot currency must be USD")

    source_head = snapshot.get("source_head")
    if (
        not isinstance(source_head, dict)
        or set(source_head) != {"paper", "live", "market"}
    ):
        raise ValidationError(
            "portfolio snapshot source_head must contain paper, live, and market"
        )
    source_count = 0
    for name in ("paper", "live", "market"):
        head = source_head[name]
        if not isinstance(head, dict):
            raise ValidationError(f"source_head.{name} must be an object")
        count = head.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValidationError(
                f"source_head.{name}.count must be a non-negative integer"
            )
        digest = head.get("hash")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValidationError(
                f"source_head.{name}.hash must be a lowercase SHA-256 digest"
            )
        last_event_id = head.get("last_event_id")
        if count == 0:
            if last_event_id is not None:
                raise ValidationError(
                    f"source_head.{name}.last_event_id must be null when count is zero"
                )
        elif (
            not isinstance(last_event_id, str)
            or not last_event_id.startswith(f"{name}-")
        ):
            raise ValidationError(
                f"source_head.{name}.last_event_id is invalid"
            )
        source_count += count
    if revision != source_count:
        raise ValidationError(
            "portfolio snapshot revision does not match source_head counts"
        )

    _snapshot_timestamp(
        snapshot.get("generated_at"),
        label="snapshot.generated_at",
        nullable=False,
    )
    for field in ("data_as_of", "prices_as_of"):
        if field not in snapshot:
            raise ValidationError(f"portfolio snapshot is missing {field}")
        _snapshot_timestamp(snapshot[field], label=f"snapshot.{field}")
    warnings = snapshot.get("warnings")
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        raise ValidationError("portfolio snapshot warnings must be strings")

    portfolios = snapshot.get("portfolios")
    if (
        not isinstance(portfolios, dict)
        or set(portfolios) != {"paper", "live"}
    ):
        raise ValidationError(
            "portfolio snapshot portfolios must contain paper and live"
        )
    for name in ("paper", "live"):
        portfolio = portfolios[name]
        if not isinstance(portfolio, dict):
            raise ValidationError(
                f"portfolio snapshot has no {name} object"
            )
        if portfolio.get("data_status") not in {
            "OK",
            "INSUFFICIENT_DATA",
            "NO_DATA",
        }:
            raise ValidationError(f"{name} data_status is invalid")
        for field in ("holdings", "recent_trades", "daily"):
            if not isinstance(portfolio.get(field), list):
                raise ValidationError(f"{name}.{field} must be an array")
        for field in ("cash", "initial_cash"):
            if field in portfolio:
                _snapshot_decimal_or_none(
                    portfolio[field],
                    label=f"{name}.{field}",
                )

        for holding in portfolio["holdings"]:
            if not isinstance(holding, dict):
                raise ValidationError(
                    f"{name}.holdings entries must be objects"
                )
            if (
                not isinstance(holding.get("symbol"), str)
                or not SYMBOL_RE.fullmatch(holding["symbol"])
            ):
                raise ValidationError(f"{name}.holdings symbol is invalid")
            if (
                not isinstance(holding.get("instrument_id"), str)
                or not INSTRUMENT_ID_RE.fullmatch(holding["instrument_id"])
            ):
                raise ValidationError(
                    f"{name}.holdings instrument_id is invalid"
                )
            if holding.get("instrument_type") not in INSTRUMENT_TYPES:
                raise ValidationError(
                    f"{name}.holdings instrument_type is invalid"
                )
            instrument_name = holding.get("instrument_name")
            if instrument_name is not None and not isinstance(
                instrument_name,
                str,
            ):
                raise ValidationError(
                    f"{name}.holdings instrument_name is invalid"
                )
            quote_symbol = holding.get("quote_symbol")
            if quote_symbol is not None and (
                not isinstance(quote_symbol, str)
                or not SYMBOL_RE.fullmatch(quote_symbol)
            ):
                raise ValidationError(
                    f"{name}.holdings quote_symbol is invalid"
                )
            if holding.get("quote_status") not in {
                "OK",
                "MANUAL",
                "MISSING",
            }:
                raise ValidationError(
                    f"{name}.holdings quote_status is invalid"
                )
            for field in (
                "shares",
                "avg_cost",
                "cost_basis",
                "contract_multiplier",
                "current_price",
                "market_value",
                "unrealized_pnl",
                "unrealized_pnl_pct",
            ):
                if field not in holding:
                    raise ValidationError(
                        f"{name}.holdings is missing {field}"
                    )
                _snapshot_decimal_or_none(
                    holding[field],
                    label=f"{name}.holdings.{field}",
                )
            if "market_price_as_of" not in holding:
                raise ValidationError(
                    f"{name}.holdings is missing market_price_as_of"
                )
            _snapshot_timestamp(
                holding["market_price_as_of"],
                label=f"{name}.holdings.market_price_as_of",
            )

        for trade in portfolio["recent_trades"]:
            if not isinstance(trade, dict) or trade.get("action") not in {
                "BUY",
                "SELL",
                "CASH_FLOW",
                "INCOME_EXPENSE",
                "SPLIT",
            }:
                raise ValidationError(
                    f"{name}.recent_trades entry is invalid"
                )
            if not isinstance(trade.get("event_id"), str) or not trade["event_id"]:
                raise ValidationError(
                    f"{name}.recent_trades event_id is invalid"
                )
            if trade.get("portfolio") != name:
                raise ValidationError(
                    f"{name}.recent_trades contains a cross-portfolio event"
                )
            if not isinstance(trade.get("source"), str) or not trade["source"]:
                raise ValidationError(
                    f"{name}.recent_trades source is invalid"
                )
            ledger_seq = trade.get("ledger_seq")
            if (
                isinstance(ledger_seq, bool)
                or not isinstance(ledger_seq, int)
                or ledger_seq < 1
            ):
                raise ValidationError(
                    f"{name}.recent_trades ledger_seq is invalid"
                )
            _snapshot_timestamp(
                trade.get("occurred_at"),
                label=f"{name}.recent_trades.occurred_at",
                nullable=False,
            )
            _snapshot_timestamp(
                trade.get("created_at"),
                label=f"{name}.recent_trades.created_at",
                nullable=False,
            )
            if trade["action"] in {"BUY", "SELL"}:
                if (
                    not isinstance(trade.get("symbol"), str)
                    or not SYMBOL_RE.fullmatch(trade["symbol"])
                ):
                    raise ValidationError(
                        f"{name}.recent_trades symbol is invalid"
                    )
                for field in (
                    "shares",
                    "price",
                    "fee",
                    "pnl",
                    "pnl_pct",
                ):
                    if field not in trade:
                        raise ValidationError(
                            f"{name}.recent_trades is missing {field}"
                        )
                    _snapshot_decimal_or_none(
                        trade[field],
                        label=f"{name}.recent_trades.{field}",
                    )
                if "settlement_adjustment" in trade:
                    _snapshot_decimal_or_none(
                        trade["settlement_adjustment"],
                        label=(
                            f"{name}.recent_trades."
                            "settlement_adjustment"
                        ),
                    )
            elif trade["action"] == "CASH_FLOW" and trade.get("symbol") != "USD":
                raise ValidationError(
                    "CASH_FLOW snapshot symbol must be USD"
                )
            elif trade["action"] == "CASH_FLOW":
                if "amount" not in trade:
                    raise ValidationError(
                        f"{name}.recent_trades is missing amount"
                    )
                _snapshot_decimal_or_none(
                    trade["amount"],
                    label=f"{name}.recent_trades.amount",
                )
            elif trade["action"] == "INCOME_EXPENSE":
                if trade.get("income_type") not in {
                    "DIVIDEND",
                    "INTEREST",
                    "FEE",
                    "CASH_IN_LIEU",
                    "OTHER",
                }:
                    raise ValidationError(
                        f"{name}.recent_trades income_type is invalid"
                    )
                for field in (
                    "amount",
                    "gross_amount",
                    "withholding_tax",
                    "pnl",
                    "pnl_pct",
                ):
                    if field not in trade:
                        raise ValidationError(
                            f"{name}.recent_trades is missing {field}"
                        )
                    _snapshot_decimal_or_none(
                        trade[field],
                        label=f"{name}.recent_trades.{field}",
                    )
            elif trade["action"] == "SPLIT":
                for field in (
                    "numerator",
                    "denominator",
                    "shares_before",
                    "shares_after",
                    "pnl",
                    "pnl_pct",
                ):
                    if field not in trade:
                        raise ValidationError(
                            f"{name}.recent_trades is missing {field}"
                        )
                    _snapshot_decimal_or_none(
                        trade[field],
                        label=f"{name}.recent_trades.{field}",
                    )

        for point in portfolio["daily"]:
            if not isinstance(point, dict):
                raise ValidationError(
                    f"{name}.daily entries must be objects"
                )
            _snapshot_date(point.get("date"), label=f"{name}.daily.date")
            if point.get("data_status") not in {
                "OK",
                "INSUFFICIENT_DATA",
                "INSUFFICIENT_MARKET_DATA",
            }:
                raise ValidationError(f"{name}.daily data_status is invalid")
            for field in (
         ïo;¶‰žËkºwµçe±ä¹…ÁÁ•¹ (€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰‘…Ñ”ˆèÍ•ÍÍ¥½¸°(€€€€€€€€€€€€€€€€€€€€‰¹…Øˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰…Í ˆè…Í °(€€€€€€€€€€€€€€€€€€€€‰•áÑ•É¹…±}™±½Üˆè•áÑ•É¹…±}™±½Ü°(€€€€€€€€€€€€€€€€€€€€‰‘…¥±å}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰ÕµÕ±…Ñ¥Ù•}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}¥ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰Á¹°ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰‘…Ñ…}ÍÑ…ÑÕÌˆè€‰%9MU%%9Q}5I-Q}Qˆ°(€€€€€€€€€€€€€€€€€€€€‰µ¥ÍÍ¥¹}Íåµ‰½±Ìˆèµ¥ÍÍ¥¹}Íåµ‰½±Ì°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€¤(€€€€€€€€€€€ÁÉ•Ù¥½ÕÍ}¹…Ø€ô9½¹”(€€€€€€€€€€€ÁÉ•Ù¥½ÕÍ}Í•µ•¹Ñ}É•ÑÕÉ¸€ô9½¹”(€€€€€€€€€€€…Á}Í••¸€ôQÉÕ”(€€€€€€€€€€€½¹Ñ¥¹Õ”((€€€€€€€µ…É­•Ñ}Ù…±Õ”€ôµ½¹•ä (€€€€€€€€€€€ÍÕ´ (€€€€€€€€€€€€€€€€ (€€€€€€€€€€€€€€€€€€€…µ½Õ¹Ñ}™½È (€€€€€€€€€€€€€€€€€€€€€€€ÅÕ…¹Ñ¥Ñä(€€€€€€€€€€€€€€€€€€€€€€€€¨Á½Í¥Ñ¥½¹}µ•Ñ…m¥¹ÍÑÉÕµ•¹Ñ}¥‘ul‰½¹ÑÉ…Ñ}µÕ±Ñ¥Á±¥•È‰t°(€€€€€€€€€€€€€€€€€€€€€€€}ÅÕ½Ñ•}™½É}‘…ä (€€€€€€€€€€€€€€€€€€€€€€€€€€€ÅÕ½Ñ•ÍmÁ½Í¥Ñ¥½¹}µ•Ñ…m¥¹ÍÑÉÕµ•¹Ñ}¥‘ul‰ÅÕ½Ñ•}­•ä‰ut°(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•ÍÍ¥½¸°(€€€€€€€€€€€€€€€€€€€€€€€€€€€Í•ÍÍ¥½¹}Í•Ð°(€€€€€€€€€€€€€€€€€€€€€€€€¥l‰±½Í”‰t°(€€€€€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€€€€€™½È¥¹ÍÑÉÕµ•¹Ñ}¥°ÅÕ…¹Ñ¥Ñä¥¸Á½Í¥Ñ¥½¹Ì¹¥Ñ•µÌ ¤(€€€€€€€€€€€€€€€€€€€¥˜ÅÕ…¹Ñ¥Ñä€ø€À(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€€€€€iI<°(€€€€€€€€€€€€¤(€€€€€€€€¤(€€€€€€€¹…Ø€ôµ½¹•ä¡…Í €¬µ…É­•Ñ}Ù…±Õ”¤((€€€€€€€Á¹°€ôµ½¹•ä¡¹…Ø€´É•ÍÕ±Ð¹¥¹¥Ñ¥…±}…Í €´ÕµÕ±…Ñ¥Ù•}•áÑ•É¹…±}™±½Ü¤(€€€€€€€¥˜ÁÉ•Ù¥½ÕÍ}¹…Ø¥Ì9½¹”è(€€€€€€€€€€€Í•µ•¹Ñ}¥€¬ô€Ä(€€€€€€€€€€€‘…¥±å}É•ÑÕÉ¸€ô9½¹”¥˜…Á}Í••¸•±Í”iI<(€€€€€€€€€€€Í•µ•¹Ñ}É•ÑÕÉ¸€ôiI<(€€€€€€€•±Í”è(€€€€€€€€€€€‘•¹½µ¥¹…Ñ½È€ôµ½¹•ä¡ÁÉ•Ù¥½ÕÍ}¹…Ø€¬•áÑ•É¹…±}™±½Ü¤(€€€€€€€€€€€¥˜‘•¹½µ¥¹…Ñ½È€ðô€Àè(€€€€€€€€€€€€€€€‘…¥±ä¹…ÁÁ•¹ (€€€€€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€€€€€‰‘…Ñ”ˆèÍ•ÍÍ¥½¸°(€€€€€€€€€€€€€€€€€€€€€€€€‰¹…Øˆè¹…Ø°(€€€€€€€€€€€€€€€€€€€€€€€€‰…Í ˆè…Í °(€€€€€€€€€€€€€€€€€€€€€€€€‰•áÑ•É¹…±}™±½Üˆè•áÑ•É¹…±}™±½Ü°(€€€€€€€€€€€€€€€€€€€€€€€€‰‘…¥±å}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€€€€€‰ÕµÕ±…Ñ¥Ù•}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}¥ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€€€€€‰Á¹°ˆèÁ¹°°(€€€€€€€€€€€€€€€€€€€€€€€€‰‘…Ñ…}ÍÑ…ÑÕÌˆè€‰%9MU%%9Q}Qˆ°(€€€€€€€€€€€€€€€€€€€€€€€€‰µ¥ÍÍ¥¹}Íåµ‰½±Ìˆèmt°(€€€€€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€€€€€ÁÉ•Ù¥½ÕÍ}¹…Ø€ô9½¹”(€€€€€€€€€€€€€€€ÁÉ•Ù¥½ÕÍ}Í•µ•¹Ñ}É•ÑÕÉ¸€ô9½¹”(€€€€€€€€€€€€€€€…Á}Í••¸€ôQÉÕ”(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€€€€€‘…¥±å}É•ÑÕÉ¸€ôÁ•É•¹Ð¡¹…Ø€¼‘•¹½µ¥¹…Ñ½È€´€Ä¤(€€€€€€€€€€€‰…Í”€ôÁÉ•Ù¥½ÕÍ}Í•µ•¹Ñ}É•ÑÕÉ¸¥˜ÁÉ•Ù¥½ÕÍ}Í•µ•¹Ñ}É•ÑÕÉ¸¥Ì¹½Ð9½¹”•±Í”iI<(€€€€€€€€€€€Í•µ•¹Ñ}É•ÑÕÉ¸€ôÁ•É•¹Ð  Ä€¬‰…Í”¤€¨€ Ä€¬‘…¥±å}É•ÑÕÉ¸¤€´€Ä¤((€€€€€€€ÕµÕ±…Ñ¥Ù•}É•ÑÕÉ¸€ô9½¹”¥˜…Á}Í••¸•±Í”Í•µ•¹Ñ}É•ÑÕÉ¸(€€€€€€€‘…¥±ä¹…ÁÁ•¹ (€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰‘…Ñ”ˆèÍ•ÍÍ¥½¸°(€€€€€€€€€€€€€€€€‰¹…Øˆè¹…Ø°(€€€€€€€€€€€€€€€€‰…Í ˆè…Í °(€€€€€€€€€€€€€€€€‰•áÑ•É¹…±}™±½Üˆè•áÑ•É¹…±}™±½Ü°(€€€€€€€€€€€€€€€€‰‘…¥±å}É•ÑÕÉ¸ˆè‘…¥±å}É•ÑÕÉ¸°(€€€€€€€€€€€€€€€€‰ÕµÕ±…Ñ¥Ù•}É•ÑÕÉ¸ˆèÕµÕ±…Ñ¥Ù•}É•ÑÕÉ¸°(€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}¥ˆèÍ•µ•¹Ñ}¥°(€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}É•ÑÕÉ¸ˆèÍ•µ•¹Ñ}É•ÑÕÉ¸°(€€€€€€€€€€€€€€€€‰Á¹°ˆèÁ¹°°(€€€€€€€€€€€€€€€€‰‘…Ñ…}ÍÑ…ÑÕÌˆè€‰=,ˆ°(€€€€€€€€€€€€€€€€‰µ¥ÍÍ¥¹}Íåµ‰½±Ìˆèmt°(€€€€€€€€€€€ô(€€€€€€€€¤(€€€€€€€ÁÉ•Ù¥½ÕÍ}¹…Ø€ô¹…Ø(€€€€€€€ÁÉ•Ù¥½ÕÍ}Í•µ•¹Ñ}É•ÑÕÉ¸€ôÍ•µ•¹Ñ}É•ÑÕÉ¸((€€€É•ÑÕÉ¸‘…¥±ä(()‘•˜}‰•¹¡µ…É­}Í•É¥•Ì (€€€‰•¹¡µ…É¬è‘¥ÑmÍÑÈ°‘¥ÑmÍÑÈ°¹åut°(€€€‘…åÌè±¥ÍÑmÍÑÉt°(€€€Í•ÍÍ¥½¹Ìè±¥ÍÑmÍÑÉt°(¤€´ø±¥ÍÑm‘¥ÑmÍÑÈ°¹åutè(€€€Í•É¥•Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€Í•ÍÍ¥½¹}Í•Ð€ôÍ•Ð¡Í•ÍÍ¥½¹Ì¤(€€€ÁÉ•Ù¥½ÕÌè•¥µ…°ð9½¹”€ô9½¹”(€€€Í•µ•¹Ñ}É•ÑÕÉ¸è•¥µ…°ð9½¹”€ô9½¹”(€€€Í•µ•¹Ñ}¥€ô€À(€€€…Á}Í••¸€ô…±Í”(€€€™½ÈÍ•ÍÍ¥½¸¥¸‘…åÌè(€€€€€€€É•½É€ô}ÅÕ½Ñ•}™½É}‘…ä¡‰•¹¡µ…É¬°Í•ÍÍ¥½¸°Í•ÍÍ¥½¹}Í•Ð¤(€€€€€€€¥˜É•½É¥Ì9½¹”è(€€€€€€€€€€€Í•É¥•Ì¹…ÁÁ•¹ (€€€€€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€€€€€‰‘…Ñ”ˆèÍ•ÍÍ¥½¸°(€€€€€€€€€€€€€€€€€€€€‰±½Í”ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰‘…¥±å}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰ÕµÕ±…Ñ¥Ù•}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}¥ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰‘…Ñ…}ÍÑ…ÑÕÌˆè€‰%9MU%%9Q}5I-Q}Qˆ°(€€€€€€€€€€€€€€€ô(€€€€€€€€€€€€¤(€€€€€€€€€€€ÁÉ•Ù¥½ÕÌ€ô9½¹”(€€€€€€€€€€€Í•µ•¹Ñ}É•ÑÕÉ¸€ô9½¹”(€€€€€€€€€€€…Á}Í••¸€ôQÉÕ”(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€±½Í”€ôÉ•½É‘l‰±½Í”‰t(€€€€€€€¥˜ÁÉ•Ù¥½ÕÌ¥Ì9½¹”è(€€€€€€€€€€€Í•µ•¹Ñ}¥€¬ô€Ä(€€€€€€€€€€€‘…¥±å}É•ÑÕÉ¸€ô9½¹”(€€€€€€€€€€€Í•µ•¹Ñ}É•ÑÕÉ¸€ôiI<(€€€€€€€•±Í”è(€€€€€€€€€€€‘…¥±å}É•ÑÕÉ¸€ôÁ•É•¹Ð¡±½Í”€¼ÁÉ•Ù¥½ÕÌ€´€Ä¤(€€€€€€€€€€€Í•µ•¹Ñ}É•ÑÕÉ¸€ôÁ•É•¹Ð (€€€€€€€€€€€€€€€€ Ä€¬€¡Í•µ•¹Ñ}É•ÑÕÉ¸¥˜Í•µ•¹Ñ}É•ÑÕÉ¸¥Ì¹½Ð9½¹”•±Í”iI<¤¤(€€€€€€€€€€€€€€€€¨€ Ä€¬‘…¥±å}É•ÑÕÉ¸¤(€€€€€€€€€€€€€€€€´€Ä(€€€€€€€€€€€€¤(€€€€€€€Í•É¥•Ì¹…ÁÁ•¹ (€€€€€€€€€€€ì(€€€€€€€€€€€€€€€€‰‘…Ñ”ˆèÍ•ÍÍ¥½¸°(€€€€€€€€€€€€€€€€‰±½Í”ˆè±½Í”°(€€€€€€€€€€€€€€€€‰‘…¥±å}É•ÑÕÉ¸ˆè‘…¥±å}É•ÑÕÉ¸°(€€€€€€€€€€€€€€€€‰ÕµÕ±…Ñ¥Ù•}É•ÑÕÉ¸ˆè9½¹”¥˜…Á}Í••¸•±Í”Í•µ•¹Ñ}É•ÑÕÉ¸°(€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}¥ˆèÍ•µ•¹Ñ}¥°(€€€€€€€€€€€€€€€€‰Í•µ•¹Ñ}É•ÑÕÉ¸ˆèÍ•µ•¹Ñ}É•ÑÕÉ¸°(€€€€€€€€€€€€€€€€‰‘…Ñ…}ÍÑ…ÑÕÌˆè€‰=,ˆ°(€€€€€€€€€€€ô(€€€€€€€€¤(€€€€€€€ÁÉ•Ù¥½ÕÌ€ô±½Í”(€€€É•ÑÕÉ¸Í•É¥•Ì(()‘•˜}Á½ÉÑ™½±¥½}…±•¹‘…È (€€€É•ÍÕ±ÐèI•Á±…åI•ÍÕ±Ð°(€€€µ…É­•Ñ}Í•ÍÍ¥½¹Ìè±¥ÍÑmÍÑÉt°(¤€´øÑÕÁ±•m±¥ÍÑmÍÑÉt°±¥ÍÑmÍÑÉutè(€€€€ˆˆ‰	Õ¥±„Á½ÉÑ™½±¥¼µ±½…°…±•¹‘…È¸((€€€5…É­•ÐÍ•ÍÍ¥½¹Ì…É”Í¡…É•Í½ÕÉ”‘…Ñ„°‰ÕÐ„™ÕÑÕÉ”µ‘…Ñ•A…Á•È•Ù•¹ÐµÕÍÐ(€€€¹½Ð•áÑ•¹1¥Ù”€¡½ÈÑ¡”‰•¹¡µ…É¬¤¥¹Ñ¼„Í•ÍÍ¥½¸™½ÈÝ¡¥ ¹¼ÅÕ½Ñ”•á¥ÍÑÌ¸(€€€… Á½ÉÑ™½±¥¼µ…ä•áÑ•¹½¹±ä¥ÑÌ½Ý¸Ñ•Éµ¥¹…°Í•ÍÍ¥½¸¸(€€€€ˆˆˆ((€€€…¹‘¥‘…Ñ•Ì€ôÍ•Ð¡µ…É­•Ñ}Í•ÍÍ¥½¹Ì¤(€€€…¹‘¥‘…Ñ•Ì¹ÕÁ‘…Ñ” (€€€€€€€}•Ù•¹Ñ}Í•ÍÍ¥½¹}…¹‘¥‘…Ñ”¡•Ù•¹Ð¤(€€€€€€€™½È•Ù•¹Ð¥¸É•ÍÕ±Ð¹•™™•Ñ¥Ù•}•Ù•¹ÑÌ(€€€€¤(€€€¥˜¹½Ð…¹‘¥‘…Ñ•Ìè(€€€€€€€É•ÑÕÉ¸mt°mt(€€€Í•ÍÍ¥½¹Ì€ô¹åÍ•}Í•ÍÍ¥½¹Ì (€€€€€€€‘…Ñ”¹™É½µ¥Í½™½Éµ…Ð¡µ¥¸¡…¹‘¥‘…Ñ•Ì¤¤°(€€€€€€€‘…Ñ”¹™É½µ¥Í½™½Éµ…Ð¡µ…à¡…¹‘¥‘…Ñ•Ì¤¤°(€€€€¤(€€€‘…åÌ€ô}…±•¹‘…É}‘…åÌ (€€€€€€€‘…Ñ”¹™É½µ¥Í½™½Éµ…Ð¡Í•ÍÍ¥½¹ÍlÁt¤°(€€€€€€€‘…Ñ”¹™É½µ¥Í½™½Éµ…Ð¡Í•ÍÍ¥½¹Íl´Åt¤°(€€€€¤(€€€É•ÑÕÉ¸Í•ÍÍ¥½¹Ì°‘…åÌ(()‘•˜}‰Õ¥±‘}Í¹…ÁÍ¡½Ñ}±½­• (€€€É½½Ñ}Á…Ñ èA…Ñ °(€€€ÍÑ½É”è1•‘•ÉMÑ½É”°(€€€€¨°(€€€½ÕÑÁÕÐèÍÑÈðA…Ñ ð9½¹”€ô9½¹”°(€€€ÝÉ¥Ñ”è‰½½°€ôQÉÕ”°(€€€É•Á…¥É}É•½É‘Ìè±¥ÍÑm‘¥ÑmÍÑÈ°¹åutð9½¹”€ô9½¹”°(¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€É•Á…¥ÉÌ€ôÉ•Á…¥É}É•½É‘Ì¥˜É•Á…¥É}É•½É‘Ì¥Ì¹½Ð9½¹”•±Í”mt(€€€Á…Á•É}•Ù•¹ÑÌ€ôÍÑ½É”¹É•… (€€€€€€€€‰Á…Á•Èˆ°(€€€€€€€É•Á…¥É}Ñ…¥°õQÉÕ”°(€€€€€€€É•Á…¥É}É•½É‘ÌõÉ•Á…¥ÉÌ°(€€€€¤(€€€±¥Ù•}•Ù•¹ÑÌ€ôÍÑ½É”¹É•… (€€€€€€€€‰±¥Ù”ˆ°(€€€€€€€É•Á…¥É}Ñ…¥°õQÉÕ”°(€€€€€€€É•Á…¥É}É•½É‘ÌõÉ•Á…¥ÉÌ°(€€€€¤(€€€µ…É­•Ñ}•Ù•¹ÑÌ€ôÍÑ½É”¹É•… (€€€€€€€€‰µ…É­•Ðˆ°(€€€€€€€É•Á…¥É}Ñ…¥°õQÉÕ”°(€€€€€€€É•Á…¥É}É•½É‘ÌõÉ•Á…¥ÉÌ°(€€€€¤(€€€}…ÍÍ•ÉÑ}Á•¹‘¥¹}‰…Ñ¡}½µÁ±•Ñ” (€€€€€€€É½½Ñ}Á…Ñ °(€€€€€€€Á…Á•É}•Ù•¹ÑÌ€¬±¥Ù•}•Ù•¹ÑÌ€¬µ…É­•Ñ}•Ù•¹ÑÌ°(€€€€¤(€€€ÅÕ½Ñ•Ì°‰•¹¡µ…É¬°µ…É­•Ñ}Í•ÍÍ¥½¹Ì€ô}µ…É­•Ñ}‘…Ñ„¡µ…É­•Ñ}•Ù•¹ÑÌ¤(€€€É•Á±…å}É•ÍÕ±ÑÌ€ôì(€€€€€€€¹…µ”èÉ•Á±…å}Á½ÉÑ™½±¥¼¡•Ù•¹ÑÌ°Á½ÉÑ™½±¥¼õ¹…µ”¤(€€€€€€€™½È¹…µ”°•Ù•¹ÑÌ¥¸€  ‰Á…Á•Èˆ°Á…Á•É}•Ù•¹ÑÌ¤°€ ‰±¥Ù”ˆ°±¥Ù•}•Ù•¹ÑÌ¤¤(€€€€€€€¥˜•Ù•¹ÑÌ(€€€ô(€€€‰•¹¡µ…É­}‘…åÌ€ô€ (€€€€€€€}…±•¹‘…É}‘…åÌ (€€€€€€€€€€€‘…Ñ”¹™É½µ¥Í½™½Éµ…Ð¡µ…É­•Ñ}Í•ÍÍ¥½¹ÍlÁt¤°(€€€€€€€€€€€‘…Ñ”¹™É½µ¥Í½™½Éµ…Ð¡µ…É­•Ñ}Í•ÍÍ¥½¹Íl´Åt¤°(€€€€€€€€¤(€€€€€€€¥˜µ…É­•Ñ}Í•ÍÍ¥½¹Ì(€€€€€€€•±Í”mt(€€€€¤(€€€Ý…É¹¥¹Ì€ôl(€€€€€€€€ (€€€€€€€€€€€˜‰íA…Ñ ¡É•Á…¥Él±•‘•Èt¤¹ÍÑ•µô±•‘•ÈÑ…¥°É•Á…¥É•ì€ˆ(€€€€€€€€€€€˜‰íÉ•Á…¥Él‰åÑ•Í}ÅÕ…É…¹Ñ¥¹•uô‰åÑ•ÌÅÕ…É…¹Ñ¥¹•ˆ(€€€€€€€€¤(€€€€€€€™½ÈÉ•Á…¥È¥¸É•Á…¥ÉÌ(€€€t(€€€Á½ÉÑ™½±¥½Ìè‘¥ÑmÍÑÈ°¹åt€ôíô((€€€™½È¹…µ”°•Ù•¹ÑÌ¥¸€  ‰Á…Á•Èˆ°Á…Á•É}•Ù•¹ÑÌ¤°€ ‰±¥Ù”ˆ°±¥Ù•}•Ù•¹ÑÌ¤¤è(€€€€€€€¥˜¹½Ð•Ù•¹ÑÌè(€€€€€€€€€€€Ý…É¹¥¹Ì¹…ÁÁ•¹¡˜‰í¹…µ•ôÁ½ÉÑ™½±¥¼¡…Ì¹¼•Ù•¹ÑÌˆ¤(€€€€€€€€€€€Á½ÉÑ™½±¥½Ím¹…µ•t€ôì(€€€€€€€€€€€€€€€€‰‘…Ñ…}ÍÑ…ÑÕÌˆè€‰9=}Qˆ°(€€€€€€€€€€€€€€€€‰¡½±‘¥¹Ìˆèmt°(€€€€€€€€€€€€€€€€‰É••¹Ñ}ÑÉ…‘•Ìˆèmt°(€€€€€€€€€€€€€€€€‰‘…¥±äˆèmt°(€€€€€€€€€€€€€€€€‰µ•ÑÉ¥Ìˆèì(€€€€€€€€€€€€€€€€€€€€‰‘…Ñ…}ÍÑ…ÑÕÌˆè€‰9=}Qˆ°(€€€€€€€€€€€€€€€€€€€€‰Á•É™½Éµ…¹•}•™™•Ñ¥Ù•}‘…Ñ”ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰Á•É™½Éµ…¹•}Í½Á”ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰Ñ½Ñ…±}É•ÑÕÉ¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰É•…±¥é•‘}Á¹°ˆè€ˆÀˆ°(€€€€€€€€€€€€€€€€€€€€‰¥¹½µ•}•áÁ•¹Í”ˆè€ˆÀˆ°(€€€€€€€€€€€€€€€€€€€€‰Ý¥¹}É…Ñ”ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰±½Í•‘}•Á¥Í½‘•Ìˆè€À°(€€€€€€€€€€€€€€€€€€€€‰µ…á}‘É…Ý‘½Ý¸ˆè9½¹”°(€€€€€€€€€€€€€€€€€€€€‰Í¡…ÉÁ•}É…Ñ¥¼ˆè9½¹”°(€€€€€€€€€€€€€€€ô°(€€€€€€€€€€€ô(€€€€€€€€€€€½¹Ñ¥¹Õ”((€€€€€€€É•ÍÕ±Ð€ôÉ•Á±…å}É•ÍÕ±ÑÍm¹…µ•t(€€€€€€€Á½ÉÑ™½±¥½}Í•ÍÍ¥½¹Ì°Á½ÉÑ™½±¥½}‘…åÌ€ô}Á½ÉÑ™½±¥½}…±•¹‘…È (€€€€€€€€€€€É•ÍÕ±Ð°(€€€€€€€€€€€µ…É­•Ñ}Í•ÍÍ¥½¹Ì°(€€€€€€€€¤(€€€€€€€‘…¥±ä€ô}‘…¥±å}Í•É¥•Ì (€€€€€€€€€€€É•ÍÕ±Ð°(€€€€€€€€€€€ÅÕ½Ñ•Ì°(€€€€€€€€€€€Á½ÉÑ™½±¥½}‘…åÌ°(€€€€€€€€€€€Á½ÉÑ™½±¥½}Í•ÍÍ¥½¹Ì°(€€€€€€€€¤(€€€€€€€±…ÍÑ}‘…ä€ôÁ½ÉÑ™½±¥½}‘…åÍl´Åt¥˜Á½ÉÑ™½±¥½}‘…åÌ•±Í”9½¹”(€€€€€€€¡½±‘¥¹Ì€ô}•¹É¥¡}¡½±‘¥¹Ì (€€€€€€€€€€€É•ÍÕ±Ð°(€€€€€€€€€€€ÅÕ½Ñ•Ì°(€€€€€€€€€€€±…ÍÑ}‘…ä°(€€€€€€€€€€€Í•Ð¡Á½ÉÑ™½±¥½}Í•ÍÍ¥½¹Ì¤°(€€€€€€€€¤(€€€€€€€µ•ÑÉ¥Ì€ô}µ•ÑÉ¥Ì¡‘…¥±ä°É•ÍÕ±Ð¤(€€€€€€€¥˜µ•ÑÉ¥Íl‰‘…Ñ…}ÍÑ…ÑÕÌ‰t€„ô€‰=,ˆè(€€€€€€€€€€€Ý…É¹¥¹Ì¹…ÁÁ•¹¡˜‰í¹…µ•ôÁ•É™½Éµ…¹”½¹Ñ…¥¹Ì¥¹½µÁ±•Ñ”µ…É­•Ð‘…Ñ„ˆ¤(€€€€€€€•±¥˜µ•ÑÉ¥Íl‰Á•É™½Éµ…¹•}Í½Á”‰t€ôô€‰1QMQ}=5A1Q}M59Pˆè(€€€€€€€€€€€Ý…É¹¥¹Ì¹…ÁÁ•¹ (€€€€€€€€€€€€€€€˜‰í¹…µ•ôÁ•É™½Éµ…¹”ÍÑ…ÉÑÌ…Ð€ˆ(€€€€€€€€€€€€€€€˜‰íµ•ÑÉ¥ÍlÁ•É™½Éµ…¹•}•™™•Ñ¥Ù•}‘…Ñ”uô…™Ñ•È¥¹½µÁ±•Ñ”€ˆ(€€€€€€€€€€€€€€€€‰µ…É­•Ð‘…Ñ„ˆ(€€€€€€€€€€€€¤(€€€€€€€Á½ÉÑ™½±¥½Ím¹…µ•t€ôì(€€€€€€€€€€€€‰‘…Ñ…}ÍÑ…ÑÕÌˆèµ•ÑÉ¥Íl‰‘…Ñ…}ÍÑ…ÑÕÌ‰t°(€€€€€€€€€€€€‰…Í ˆèÉ•ÍÕ±Ð¹…Í °(€€€€€€€€€€€€‰¥¹¥Ñ¥…±}…Í ˆèÉ•ÍÕ±Ð¹¥¹¥Ñ¥…±}…Í °(€€€€€€€€€€€€‰¡½±‘¥¹Ìˆè¡½±‘¥¹Ì°(€€€€€€€€€€€€‰É••¹Ñ}ÑÉ…‘•Ìˆè±¥ÍÐ¡É•Ù•ÉÍ•¡É•ÍÕ±Ð¹ÑÉ…‘•}¡¥ÍÑ½Éä¤¤°(€€€€€€€€€€€€‰É•…±¥é•‘}Á¹±}Á•É}ÑÉ…‘”ˆèÉ•ÍÕ±Ð¹É•…±¥é•‘}Á¹±}Á•É}ÑÉ…‘”°(€€€€€€€€€€€€‰‘…¥±äˆè‘…¥±ä°(€€€€€€€€€€€€‰µ•ÑÉ¥Ìˆèµ•ÑÉ¥Ì°(€€€€€€€ô((€€€Í½ÕÉ•}¡•…€ôì(€€€€€€€€‰Á…Á•Èˆè}Í½ÕÉ•}¡•…¡ÍÑ½É”¹Á…Ñ¡}™½È ‰Á…Á•Èˆ¤°Á…Á•É}•Ù•¹ÑÌ¤°(€€€€€€€€‰±¥Ù”ˆè}Í½ÕÉ•}¡•…¡ÍÑ½É”¹Á…Ñ¡}™½È ‰±¥Ù”ˆ¤°±¥Ù•}•Ù•¹ÑÌ¤°(€€€€€€€€‰µ…É­•Ðˆè}Í½ÕÉ•}¡•…¡ÍÑ½É”¹Á…Ñ¡}™½È ‰µ…É­•Ðˆ¤°µ…É­•Ñ}•Ù•¹ÑÌ¤°(€€€ô(€€€…±±}•Ù•¹ÑÌ€ôÁ…Á•É}•Ù•¹ÑÌ€¬±¥Ù•}•Ù•¹ÑÌ€¬µ…É­•Ñ}•Ù•¹ÑÌ(€€€±…Ñ•ÍÑ}•Ù•¹Ð€ôµ…à (€€€€€€€…±±}•Ù•¹ÑÌ°(€€€€€€€­•äõ±…µ‰‘„•Ù•¹ÐèÁ…ÉÍ•}Ñ¥µ•ÍÑ…µÀ (€€€€€€€€€€€•Ù•¹Ñl‰½ÕÉÉ•‘}…Ð‰t°(€€€€€€€€€€€™¥•±ô‰½ÕÉÉ•‘}…Ðˆ°(€€€€€€€€¤°(€€€€€€€‘•™…Õ±Ðõ9½¹”°(€€€€¤(€€€±…Ñ•ÍÑ}µ…É­•Ñ}•Ù•¹Ð€ôµ…à (€€€€€€€µ…É­•Ñ}•Ù•¹ÑÌ°(€€€€€€€­•äõ±…µ‰‘„•Ù•¹ÐèÁ…ÉÍ•}Ñ¥µ•ÍÑ…µÀ (€€€€€€€€€€€•Ù•¹Ñl‰½ÕÉÉ•‘}…Ð‰t°(€€€€€€€€€€€™¥•±ô‰½ÕÉÉ•‘}…Ðˆ°(€€€€€€€€¤°(€€€€€€€‘•™…Õ±Ðõ9½¹”°(€€€€¤(€€€±…Ñ•ÍÑ}•Ù•¹Ñ}Ñ¥µ”€ô±…Ñ•ÍÑ}•Ù•¹Ñl‰½ÕÉÉ•‘}…Ð‰t¥˜±…Ñ•ÍÑ}•Ù•¹Ð•±Í”9½¹”(€€€ÁÉ¥•Í}…Í}½˜€ô€ (€€€€€€€±…Ñ•ÍÑ}µ…É­•Ñ}•Ù•¹Ñl‰½ÕÉÉ•‘}…Ð‰t¥˜±…Ñ•ÍÑ}µ…É­•Ñ}•Ù•¹Ð•±Í”9½¹”(€€€€¤(€€€É•½É‘•‘}µ…É­•Ñ}Í•ÍÍ¥½¹Ì€ôÍ½ÉÑ• (€€€€€€€ì(€€€€€€€€€€€•Ù•¹Ñl‰Í•ÍÍ¥½¹}‘…Ñ”‰t(€€€€€€€€€€€™½È•Ù•¹Ð¥¸µ…É­•Ñ}•Ù•¹ÑÌ(€€€€€€€€€€€¥˜•Ù•¹Ñl‰…Ñ¥½¸‰t¥¸ì‰EU=Qˆ°€‰	9!5I-}1=M‰ô(€€€€€€€ô(€€€€¤(€€€¥˜É•½É‘•‘}µ…É­•Ñ}Í•ÍÍ¥½¹Ìè(€€€€€€€±…Ñ•ÍÑ}½µÁ±•Ñ•€ô±…Ñ•ÍÑ}½µÁ±•Ñ•‘}¹åÍ•}Í•ÍÍ¥½¸¡‘…Ñ•Ñ¥µ”¹¹½Ü¡UQ¤¤(€€€€€€€•áÁ•Ñ•‘}Í•ÍÍ¥½¹Ì€ô¹åÍ•}Í•ÍÍ¥½¹Ì (€€€€€€€€€€€‘…Ñ”¹™É½µ¥Í½™½Éµ…Ð¡É•½É‘•‘}µ…É­•Ñ}Í•ÍÍ¥½¹Íl´Åt¤(€€€€€€€€€€€€¬Ñ¥µ•‘•±Ñ„¡‘…åÌôÄ¤°(€€€€€€€€€€€±…Ñ•ÍÑ}½µÁ±•Ñ•°(€€€€€€€€¤(€€€€€€€¥˜±•¸¡•áÁ•Ñ•‘}Í•ÍÍ¥½¹Ì¤€ø€Äè(€€€€€€€€€€€Ý…É¹¥¹Ì¹…ÁÁ•¹ ‰ÁÉ¥•Ì€ø€ÄÑÉ…‘¥¹œ‘…äÍÑ…±”ˆ¤(€€€É•Ù¥Í¥½¸€ôÍÕ´¡¡•…‘l‰½Õ¹Ð‰t™½È¡•…¥¸Í½ÕÉ•}¡•…¹Ù…±Õ•Ì ¤¤(€€€Í¹…ÁÍ¡½Ð€ô©Í½¹}Í…™” (€€€€€€€ì(€€€€€€€€€€€€‰Í¡•µ…}Ù•ÉÍ¥½¸ˆè€Ð°(€€€€€€€€€€€€‰É•Ù¥Í¥½¸ˆèÉ•Ù¥Í¥½¸°(€€€€€€€€€€€€‰•¹•É…Ñ•‘}…Ðˆè‘…Ñ•Ñ¥µ”¹¹½Ü¡UQ¤¹¥Í½™½Éµ…Ð ¤¹É•Á±…” ˆ¬ÀÀèÀÀˆ°€‰hˆ¤°(€€€€€€€€€€€€‰‘…Ñ…}…Í}½˜ˆè±…Ñ•ÍÑ}•Ù•¹Ñ}Ñ¥µ”°(€€€€€€€€€€€€‰ÁÉ¥•Í}…Í}½˜ˆèÁÉ¥•Í}…Í}½˜°(€€€€€€€€€€€€‰ÕÉÉ•¹äˆè€‰UMˆ°(€€€€€€€€€€€€‰Í½ÕÉ•}¡•…ˆèÍ½ÕÉ•}¡•…°(€€€€€€€€€€€€‰Á½ÉÑ™½±¥½ÌˆèÁ½ÉÑ™½±¥½Ì°(€€€€€€€€€€€€‰‰•¹¡µ…É¬ˆèì(€€€€€€€€€€€€€€€€‰Íåµ‰½°ˆè€‰MAdˆ°(€€€€€€€€€€€€€€€€‰‘…¥±äˆè}‰•¹¡µ…É­}Í•É¥•Ì (€€€€€€€€€€€€€€€€€€€‰•¹¡µ…É¬°(€€€€€€€€€€€€€€€€€€€‰•¹¡µ…É­}‘…åÌ°(€€€€€€€€€€€€€€€€€€€µ…É­•Ñ}Í•ÍÍ¥½¹Ì°(€€€€€€€€€€€€€€€€¤°(€€€€€€€€€€€ô°(€€€€€€€€€€€€‰Ý…É¹¥¹ÌˆèÝ…É¹¥¹Ì°(€€€€€€€ô(€€€€¤(€€€Ù…±¥‘…Ñ•}Í¹…ÁÍ¡½Ð¡Í¹…ÁÍ¡½Ð¤((€€€¥˜ÝÉ¥Ñ”è(€€€€€€€Ñ…É•Ð€ôA…Ñ ¡½ÕÑÁÕÐ¤¥˜½ÕÑÁÕÐ•±Í”É½½Ñ}Á…Ñ €¼€‰Í¹…ÁÍ¡½ÑÌˆ€¼€‰Á½ÉÑ™½±¥¼µÍ¹…ÁÍ¡½Ð¹©Í½¸ˆ(€€€€€€€…Ñ½µ¥}ÝÉ¥Ñ•}©Í½¸¡Ñ…É•Ð°Í¹…ÁÍ¡½Ð°µ½‘”ôÁ¼ØÀÀ¤(€€€É•ÑÕÉ¸Í¹…ÁÍ¡½Ð(()‘•˜‰Õ¥±‘}Í¹…ÁÍ¡½Ð (€€€É½½ÐèÍÑÈðA…Ñ °(€€€€¨°(€€€½ÕÑÁÕÐèÍÑÈðA…Ñ ð9½¹”€ô9½¹”°(€€€ÝÉ¥Ñ”è‰½½°€ôQÉÕ”°(¤€´ø‘¥ÑmÍÑÈ°¹åtè(€€€€ˆˆ‰	Õ¥±½¹”½¹Í¥ÍÑ•¹ÐÁÕ‰±¥ŒÍ¹…ÁÍ¡½ÐÕ¹‘•ÈÑ¡”±½‰…°±•‘•È±½¬¸((€€€±•…É¥¹œÉ•‰Õ¥±¹Á•¹‘¥¹œÝ¡¥±”Ñ¡”Í…µ”±½¬¥Ì¡•±ÁÉ•Ù•¹ÑÌ„½¹ÕÉÉ•¹Ð(€€€…ÁÁ•¹™É½´¡…Ù¥¹œ¥ÑÌ¹•Ý•ÈÉ•½Ù•Éäµ…É­•È…¥‘•¹Ñ…±±äÉ•µ½Ù•¸(€€€€ˆˆˆ((€€€É½½Ñ}Á…Ñ €ôA…Ñ ¡É½½Ð¤(€€€ÍÑ½É”€ô1•‘•ÉMÑ½É”¡É½½Ñ}Á…Ñ ¤(€€€Ý¥Ñ ¥±•1½¬¡ÍÑ½É”¹±½­}Á…Ñ ¤è(€€€€€€€Í¹…ÁÍ¡½Ð€ô}‰Õ¥±‘}Í¹…ÁÍ¡½Ñ}±½­• (€€€€€€€€€€€É½½Ñ}Á…Ñ °(€€€€€€€€€€€ÍÑ½É”°(€€€€€€€€€€€½ÕÑÁÕÐõ½ÕÑÁÕÐ°(€€€€€€€€€€€ÝÉ¥Ñ”õÝÉ¥Ñ”°(€€€€€€€€¤(€€€€€€€¥˜ÝÉ¥Ñ”è(€€€€€€€€€€€‘ÕÉ…‰±•}Õ¹±¥¹¬¡É½½Ñ}Á…Ñ €¼€‰ÍÑ…Ñ”ˆ€¼€‰É•‰Õ¥±¹Á•¹‘¥¹œˆ¤(€€€€€€€É•ÑÕÉ¸Í¹…ÁÍ¡½Ð(()‘•˜‰Õ¥±‘}Í¹…ÁÍ¡½Ñ}¥™}¹••‘• (€€€É½½ÐèÍÑÈðA…Ñ °(€€€€¨°(€€€½ÕÑÁÕÐèÍÑÈðA…Ñ ð9½¹”€ô9½¹”°(¤€´øÑÕÁ±•m‘¥ÑmÍÑÈ°¹åt°‰½½±tè(€€€€ˆˆ‰I•‰Õ¥±½¹±äÝ¡•¸„±•‘•ÈÍ½ÕÉ”¡•…‘¥™™•ÉÌ™É½´Ñ¡”±…ÍÐÍ¹…ÁÍ¡½Ð¸ˆˆˆ((€€€É½½Ñ}Á…Ñ €ôA…Ñ ¡É½½Ð¤(€€€ÍÑ½É”€ô1•‘•ÉMÑ½É”¡É½½Ñ}Á…Ñ ¤(€€€Ñ…É•Ð€ôA…Ñ ¡½ÕÑÁÕÐ¤¥˜½ÕÑÁÕÐ•±Í”É½½Ñ}Á…Ñ €¼€‰Í¹…ÁÍ¡½ÑÌˆ€¼€‰Á½ÉÑ™½±¥¼µÍ¹…ÁÍ¡½Ð¹©Í½¸ˆ(€€€Ý¥Ñ ¥±•1½¬¡ÍÑ½É”¹±½­}Á…Ñ ¤è(€€€€€€€É•Á…¥ÉÌè±¥ÍÑm‘¥ÑmÍÑÈ°¹åut€ômt(€€€€€€€Á…Á•É}•Ù•¹ÑÌ€ôÍÑ½É”¹É•… (€€€€€€€€€€€€‰Á…Á•Èˆ°(€€€€€€€€€€€É•Á…¥É}Ñ…¥°õQÉÕ”°(€€€€€€€€€€€É•Á…¥É}É•½É‘ÌõÉ•Á…¥ÉÌ°(€€€€€€€€¤(€€€€€€€±¥Ù•}•Ù•¹ÑÌ€ôÍÑ½É”¹É•… (€€€€€€€€€€€€‰±¥Ù”ˆ°(€€€€€€€€€€€É•Á…¥É}Ñ…¥°õQÉÕ”°(€€€€€€€€€€€É•Á…¥É}É•½É‘ÌõÉ•Á…¥ÉÌ°(€€€€€€€€¤(€€€€€€€µ…É­•Ñ}•Ù•¹ÑÌ€ôÍÑ½É”¹É•… (€€€€€€€€€€€€‰µ…É­•Ðˆ°(€€€€€€€€€€€É•Á…¥É}Ñ…¥°õQÉÕ”°(€€€€€€€€€€€É•Á…¥É}É•½É‘ÌõÉ•Á…¥ÉÌ°(€€€€€€€€¤(€€€€€€€ÕÉÉ•¹Ñ}¡•…‘Ì€ôì(€€€€€€€€€€€€‰Á…Á•Èˆè}Í½ÕÉ•}¡•…¡ÍÑ½É”¹Á…Ñ¡}™½È ‰Á…Á•Èˆ¤°Á…Á•É}•Ù•¹ÑÌ¤°(€€€€€€€€€€€€‰±¥Ù”ˆè}Í½ÕÉ•}¡•…¡ÍÑ½É”¹Á…Ñ¡}™½È ‰±¥Ù”ˆ¤°±¥Ù•}•Ù•¹ÑÌ¤°(€€€€€€€€€€€€‰µ…É­•Ðˆè}Í½ÕÉ•}¡•…¡ÍÑ½É”¹Á…Ñ¡}™½È ‰µ…É­•Ðˆ¤°µ…É­•Ñ}•Ù•¹ÑÌ¤°(€€€€€€€ô(€€€€€€€}…ÍÍ•ÉÑ}Á•¹‘¥¹}‰…Ñ¡}½µÁ±•Ñ” (€€€€€€€€€€€É½½Ñ}Á…Ñ °(€€€€€€€€€€€Á…Á•É}•Ù•¹ÑÌ€¬±¥Ù•}•Ù•¹ÑÌ€¬µ…É­•Ñ}•Ù•¹ÑÌ°(€€€€€€€€¤(€€€€€€€ÕÉÉ•¹Ðè‘¥ÑmÍÑÈ°¹åtð9½¹”€ô9½¹”(€€€€€€€¥˜Ñ…É•Ð¹•á¥ÍÑÌ ¤è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€Á…ÉÍ•€ô©Í½¸¹±½…‘Ì¡Ñ…É•Ð¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤(€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡Á…ÉÍ•°‘¥Ð¤è(€€€€€€€€€€€€€€€€€€€ÕÉÉ•¹Ð€ôÁ…ÉÍ•(€€€€€€€€€€€•á•ÁÐ€¡=MÉÉ½È°©Í½¸¹)M=9•½‘•ÉÉ½È¤è(€€€€€€€€€€€€€€€ÕÉÉ•¹Ð€ô9½¹”(€€€€€€€¥˜ÕÉÉ•¹Ð¥Ì¹½Ð9½¹”è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€Ù…±¥‘…Ñ•}Í¹…ÁÍ¡½Ð¡ÕÉÉ•¹Ð¤(€€€€€€€€€€€•á•ÁÐY…±¥‘…Ñ¥½¹ÉÉ½Èè(€€€€€€€€€€€€€€€ÕÉÉ•¹Ð€ô9½¹”(€€€€€€€ÍÑÉÕÑÕÉ…±±å}ÕÉÉ•¹Ð€ô€ (€€€€€€€€€€€ÕÉÉ•¹Ð¥Ì¹½Ð9½¹”(€€€€€€€€€€€…¹ÕÉÉ•¹Ð¹•Ð ‰Í½ÕÉ•}¡•…ˆ¤€ôôÕÉÉ•¹Ñ}¡•…‘Ì(€€€€€€€€¤(€€€€€€€¥˜ÍÑÉÕÑÕÉ…±±å}ÕÉÉ•¹Ð…¹¹½ÐÉ•Á…¥ÉÌè(€€€€€€€€€€€‘ÕÉ…‰±•}Õ¹±¥¹¬¡É½½Ñ}Á…Ñ €¼€‰ÍÑ…Ñ”ˆ€¼€‰É•‰Õ¥±¹Á•¹‘¥¹œˆ¤(€€€€€€€€€€€É•ÑÕÉ¸ÕÉÉ•¹Ð°…±Í”((€€€€€€€Í¹…ÁÍ¡½Ð€ô}‰Õ¥±‘}Í¹…ÁÍ¡½Ñ}±½­• (€€€€€€€€€€€É½½Ñ}Á…Ñ °(€€€€€€€€€€€ÍÑ½É”°(€€€€€€€€€€€½ÕÑÁÕÐõÑ…É•Ð°(€€€€€€€€€€€ÝÉ¥Ñ”õQÉÕ”°(€€€€€€€€€€€É•Á…¥É}É•½É‘ÌõÉ•Á…¥ÉÌ°(€€€€€€€€¤(€€€€€€€‘ÕÉ…‰±•}Õ¹±¥¹¬¡É½½Ñ}Á…Ñ €¼€‰ÍÑ…Ñ”ˆ€¼€‰É•‰Õ¥±¹Á•¹‘¥¹œˆ¤(€€€€€€€É•ÑÕÉ¸Í¹…ÁÍ¡½Ð°QÉÕ”(