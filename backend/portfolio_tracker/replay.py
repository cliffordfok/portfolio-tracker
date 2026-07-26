"""Exact cash and FIFO replay from a resolved event stream."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from decimal import ROUND_HALF_UP
from typing import Any

from .decimal_utils import (
    MONEY_QUANT,
    ZERO,
    amount_for,
    json_safe,
    money,
    percent,
    price,
    shares,
)
from .errors import BusinessInvariantError
from .resolver import resolve_effective_events


@dataclass
class Lot:
    event_id: str
    instrument_id: str
    instrument_type: str
    instrument_name: str | None
    quote_symbol: str | None
    symbol: str
    original_shares: Decimal
    remaining_shares: Decimal
    unit_price: Decimal
    contract_multiplier: Decimal
    fee_total: Decimal
    settlement_adjustment_total: Decimal = ZERO
    fee_allocated: Decimal = ZERO
    settlement_adjustment_allocated: Decimal = ZERO

    @property
    def remaining_fee(self) -> Decimal:
        return money(self.fee_total - self.fee_allocated)

    @property
    def remaining_settlement_adjustment(self) -> Decimal:
        return money(
            self.settlement_adjustment_total
            - self.settlement_adjustment_allocated
        )

    @property
    def remaining_cost(self) -> Decimal:
        return money(
            amount_for(
                self.remaining_shares * self.contract_multiplier,
                self.unit_price,
            )
            + self.remaining_fee
            - self.remaining_settlement_adjustment
        )


@dataclass
class ReplayResult:
    portfolio: str
    initial_cash: Decimal
    cash: Decimal
    cash_flow_total: Decimal
    income_expense_total: Decimal
    buy_outflow: Decimal
    sell_inflow: Decimal
    realized_pnl_total: Decimal
    lots: dict[str, list[Lot]]
    holdings: list[dict[str, Any]]
    trade_history: list[dict[str, Any]]
    realized_pnl_per_trade: list[dict[str, Any]]
    closed_episodes: list[dict[str, Any]]
    effective_events: list[dict[str, Any]]
    raw_event_ids: set[str] = field(default_factory=set)

    @property
    def win_rate(self) -> Decimal | None:
        if not self.closed_episodes:
            return None
        wins = sum(1 for episode in self.closed_episodes if episode["pnl"] > 0)
        return percent(Decimal(wins) / Decimal(len(self.closed_episodes)))

    def to_dict(self) -> dict[str, Any]:
        return json_safe(
            {
                "portfolio": self.portfolio,
                "initial_cash": self.initial_cash,
                "cash": self.cash,
                "cash_flow_total": self.cash_flow_total,
                "income_expense_total": self.income_expense_total,
                "buy_outflow": self.buy_outflow,
                "sell_inflow": self.sell_inflow,
                "realized_pnl_total": self.realized_pnl_total,
                "holdings": self.holdings,
                "trade_history": self.trade_history,
                "realized_pnl_per_trade": self.realized_pnl_per_trade,
                "closed_episodes": self.closed_episodes,
                "win_rate": self.win_rate,
                "effective_events": self.effective_events,
            }
        )


def _allocate_fee(
    *,
    fee_total: Decimal,
    quantity: Decimal,
    original_quantity: Decimal,
    allocated_so_far: Decimal,
    is_last: bool,
    round_to_cents: bool = False,
) -> Decimal:
    if is_last:
        return money(fee_total - allocated_so_far)
    allocated = fee_total * quantity / original_quantity
    if round_to_cents:
        return allocated.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    return money(allocated)


def replay_portfolio(
    events: list[dict[str, Any]],
    *,
    portfolio: str | None = None,
    allow_negative_cash: bool | None = None,
) -> ReplayResult:
    """Rebuild all derived state from zero using exact Decimal arithmetic."""

    if not events:
        raise BusinessInvariantError("portfolio ledger is empty")
    expected_portfolio = portfolio or events[0]["portfolio"]
    if expected_portfolio not in {"paper", "live"}:
        raise BusinessInvariantError("economic replay supports paper/live only")
    if allow_negative_cash is None:
        allow_negative_cash = expected_portfolio == "live"

    effective = resolve_effective_events(events)
    opens = [event for event in effective if event["action"] == "PORTFOLIO_OPEN"]
    if len(opens) != 1:
        raise BusinessInvariantError("portfolio requires exactly one PORTFOLIO_OPEN")
    if effective[0]["action"] != "PORTFOLIO_OPEN":
        raise BusinessInvariantError("PORTFOLIO_OPEN must precede economic activity")

    initial_cash = money(opens[0]["initial_cash"], field="initial_cash")
    cash = initial_cash
    cash_flow_total = ZERO
    income_expense_total = ZERO
    buy_outflow = ZERO
    sell_inflow = ZERO
    realized_total = ZERO
    lots: dict[str, list[Lot]] = defaultdict(list)
    history: list[dict[str, Any]] = []
    realized_trades: list[dict[str, Any]] = []
    episodes: list[dict[str, Any]] = []
    episode_state: dict[str, dict[str, Any]] = {}
    running_realized = ZERO

    for event in effective:
        if event["portfolio"] != expected_portfolio:
            raise BusinessInvariantError("event belongs to a different portfolio")
        action = event["action"]
        if action == "PORTFOLIO_OPEN":
            continue

        if action == "CASH_FLOW":
            flow = money(event["amount"])
            cash = money(cash + flow)
            if not allow_negative_cash and cash < 0:
                raise BusinessInvariantError(
                    f"CASH_FLOW {event['event_id']} would make cash negative"
                )
            cash_flow_total = money(cash_flow_total + flow)
            history.append(
                {
                    **deepcopy(event),
                    "amount": flow,
                    "pnl": None,
                    "pnl_pct": None,
                }
            )
            continue

        if action == "INCOME_EXPENSE":
            amount = money(event["amount"])
            cash = money(cash + amount)
            if not allow_negative_cash and cash < 0:
                raise BusinessInvariantError(
                    f"INCOME_EXPENSE {event['event_id']} would make cash negative"
                )
            income_expense_total = money(income_expense_total + amount)
            history.append(
                {
                    **deepcopy(event),
                    "amount": amount,
                    "gross_amount": (
                        money(event["gross_amount"], field="gross_amount")
                        if "gross_amount" in event
                        else None
                    ),
                    "withholding_tax": money(
                        event["withholding_tax"],
                        field="withholding_tax",
                    ),
                    "pnl": amount,
                    "pnl_pct": None,
                }
            )
            continue

        symbol = event["symbol"]
        instrument_id = event.get("instrument_id") or symbol

        if action == "SPLIT":
            ratio = (
                shares(event["numerator"], field="numerator")
                / shares(event["denominator"], field="denominator")
            )
            open_lots = [
                lot for lot in lots[instrument_id] if lot.remaining_shares > 0
            ]
            if not open_lots:
                raise BusinessInvariantError(
                    f"SPLIT {event['event_id']} has no open position"
                )
            if any(lot.symbol != symbol for lot in open_lots) or (
                "instrument_type" in event
                and any(
                    lot.instrument_type != event["instrument_type"]
                    for lot in open_lots
                )
            ):
                raise BusinessInvariantError(
                    f"SPLIT {event['event_id']} instrument identity mismatch"
                )
            before = sum((lot.remaining_shares for lot in open_lots), ZERO)
            for lot in open_lots:
                lot.original_shares = shares(lot.original_shares * ratio)
                lot.remaining_shares = shares(lot.remaining_shares * ratio)
                lot.unit_price = price(lot.unit_price / ratio)
            after = sum((lot.remaining_shares for lot in open_lots), ZERO)
            history.append(
                {
                    **deepcopy(event),
                    "shares_before": before,
                    "shares_after": after,
                    "pnl": None,
                    "pnl_pct": None,
                }
            )
            continue

        quantity = shares(event["shares"])
        unit_price = price(event["price"])
        fee = money(event.get("fee", 0), field="fee")
        settlement_adjustment = money(
            event.get("settlement_adjustment", 0),
            field="settlement_adjustment",
        )
        contract_multiplier = shares(
            event.get("contract_multiplier", "1"),
            field="contract_multiplier",
        )

        if action == "BUY":
            cost = money(
                amount_for(quantity * contract_multiplier, unit_price) + fee
                - settlement_adjustment
            )
            cash = money(cash - cost)
            if not allow_negative_cash and cash < 0:
                raise BusinessInvariantError(
                    f"BUY {event['event_id']} would make cash negative"
                )
            existing_open_lots = [
                lot
                for lot in lots[instrument_id]
                if lot.remaining_shares > 0
            ]
            instrument_type = event.get("instrument_type", "EQUITY")
            quote_symbol = event.get("quote_symbol", symbol)
            if any(
                lot.symbol != symbol
                or lot.instrument_type != instrument_type
                or lot.quote_symbol != quote_symbol
                or lot.contract_multiplier != contract_multiplier
                for lot in existing_open_lots
            ):
                raise BusinessInvariantError(
                    f"BUY {event['event_id']} instrument identity mismatch"
                )
            if (
                sum(
                    (
                        lot.remaining_shares
                        for lot in lots[instrument_id]
                    ),
                    ZERO,
                )
                == 0
            ):
                episode_state[instrument_id] = {
                    "instrument_id": instrument_id,
                    "instrument_type": event.get("instrument_type", "EQUITY"),
                    "instrument_name": event.get("instrument_name"),
                    "quote_symbol": event.get("quote_symbol", symbol),
                    "symbol": symbol,
                    "opened_at": event["occurred_at"],
                    "opened_event_id": event["event_id"],
                    "pnl": ZERO,
                }
            lots[instrument_id].append(
                Lot(
                    event_id=event["event_id"],
                    instrument_id=instrument_id,
                    instrument_type=instrument_type,
                    instrument_name=event.get("instrument_name"),
                    quote_symbol=quote_symbol,
                    symbol=symbol,
                    original_shares=quantity,
                    remaining_shares=quantity,
                    unit_price=unit_price,
                    contract_multiplier=contract_multiplier,
                    fee_total=fee,
                    settlement_adjustment_total=settlement_adjustment,
                )
            )
            buy_outflow = money(buy_outflow + cost)
            history.append(
                {
                    **deepcopy(event),
                    "shares": quantity,
                    "price": unit_price,
                    "fee": fee,
                    **(
                        {"settlement_adjustment": settlement_adjustment}
                        if "settlement_adjustment" in event
                        else {}
                    ),
                    "pnl": None,
                    "pnl_pct": None,
                }
            )
            continue

        if action != "SELL":
            raise BusinessInvariantError(f"unexpected economic action: {action}")

        open_lots = [
            lot for lot in lots[instrument_id] if lot.remaining_shares > 0
        ]
        if any(
            lot.symbol != symbol
            or lot.contract_multiplier != contract_multiplier
            or (
                "instrument_type" in event
                and lot.instrument_type != event["instrument_type"]
            )
            or (
                "quote_symbol" in event
                and lot.quote_symbol != event["quote_symbol"]
            )
            for lot in open_lots
        ):
            raise BusinessInvariantError(
                f"SELL {event['event_id']} instrument identity mismatch"
            )
        available = sum((lot.remaining_shares for lot in open_lots), ZERO)
        if quantity > available:
            raise BusinessInvariantError(
                f"SELL {event['event_id']} oversells {symbol}: "
                f"{quantity} requested, {available} available"
            )

        gross = amount_for(quantity * contract_multiplier, unit_price)
        net = money(gross - fee + settlement_adjustment)
        cash = money(cash + net)
        sell_inflow = money(sell_inflow + net)
        remaining_to_sell = quantity
        sell_fee_allocated = ZERO
        sell_settlement_adjustment_allocated = ZERO
        trade_pnl = ZERO
        trade_cost = ZERO
        matches: list[dict[str, Any]] = []

        for lot in lots[instrument_id]:
            if remaining_to_sell <= 0:
                break
            if lot.remaining_shares <= 0:
                continue
            matched = min(lot.remaining_shares, remaining_to_sell)
            is_last_match = matched == remaining_to_sell
            buy_fee = _allocate_fee(
                fee_total=lot.fee_total,
                quantity=matched,
                original_quantity=lot.original_shares,
                allocated_so_far=lot.fee_allocated,
                is_last=matched == lot.remaining_shares,
            )
            sell_fee = _allocate_fee(
                fee_total=fee,
                quantity=matched,
                original_quantity=quantity,
                allocated_so_far=sell_fee_allocated,
                is_last=is_last_match,
                round_to_cents=True,
            )
            buy_settlement_adjustment = _allocate_fee(
                fee_total=lot.settlement_adjustment_total,
                quantity=matched,
                original_quantity=lot.original_shares,
                allocated_so_far=lot.settlement_adjustment_allocated,
                is_last=matched == lot.remaining_shares,
            )
            sell_settlement_adjustment = _allocate_fee(
                fee_total=settlement_adjustment,
                quantity=matched,
                original_quantity=quantity,
                allocated_so_far=sell_settlement_adjustment_allocated,
                is_last=is_last_match,
            )
            matched_cost = money(
                amount_for(
                    matched * lot.contract_multiplier,
                    lot.unit_price,
                )
                + buy_fee
                - buy_settlement_adjustment
            )
            matched_proceeds = money(
                amount_for(matched * contract_multiplier, unit_price)
                - sell_fee
                + sell_settlement_adjustment
            )
            matched_pnl = money(matched_proceeds - matched_cost)

            lot.remaining_shares = shares(lot.remaining_shares - matched)
            lot.fee_allocated = money(lot.fee_allocated + buy_fee)
            lot.settlement_adjustment_allocated = money(
                lot.settlement_adjustment_allocated
                + buy_settlement_adjustment
            )
            remaining_to_sell = shares(remaining_to_sell - matched)
            sell_fee_allocated = money(sell_fee_allocated + sell_fee)
            sell_settlement_adjustment_allocated = money(
                sell_settlement_adjustment_allocated
                + sell_settlement_adjustment
            )
            trade_cost = money(trade_cost + matched_cost)
            trade_pnl = money(trade_pnl + matched_pnl)
            matches.append(
                {
                    "buy_event_id": lot.event_id,
                    "instrument_id": instrument_id,
                    "shares": matched,
                    "buy_price": lot.unit_price,
                    "sell_price": unit_price,
                    "contract_multiplier": contract_multiplier,
                    "buy_fee": buy_fee,
                    "sell_fee": sell_fee,
                    "buy_settlement_adjustment": buy_settlement_adjustment,
                    "sell_settlement_adjustment": sell_settlement_adjustment,
                    "cost": matched_cost,
                    "proceeds": matched_proceeds,
                    "pnl": matched_pnl,
                }
            )

        running_realized = money(running_realized + trade_pnl)
        realized_total = money(realized_total + trade_pnl)
        pnl_pct = percent(trade_pnl / trade_cost) if trade_cost != 0 else None
        realized_entry = {
            "event_id": event["event_id"],
            "instrument_id": instrument_id,
            "instrument_type": event.get("instrument_type", "EQUITY"),
            "instrument_name": event.get("instrument_name"),
            "symbol": symbol,
            "occurred_at": event["occurred_at"],
            "shares": quantity,
            "price": unit_price,
            "fee": fee,
            **(
                {"settlement_adjustment": settlement_adjustment}
                if "settlement_adjustment" in event
                else {}
            ),
            "contract_multiplier": contract_multiplier,
            "pnl": trade_pnl,
            "pnl_pct": pnl_pct,
            "cumulative_pnl": running_realized,
            "matches": matches,
        }
        realized_trades.append(realized_entry)
        history.append(
            {
                **deepcopy(event),
                "shares": quantity,
                "price": unit_price,
                "fee": fee,
                **(
                    {"settlement_adjustment": settlement_adjustment}
                    if "settlement_adjustment" in event
                    else {}
                ),
                "pnl": trade_pnl,
                "pnl_pct": pnl_pct,
                "cumulative_pnl": running_realized,
            }
        )

        episode = episode_state.get(instrument_id)
        if episode is None:
            raise BusinessInvariantError(f"SELL {event['event_id']} has no open episode")
        episode["pnl"] = money(episode["pnl"] + trade_pnl)
        position_after = sum(
            (
                lot.remaining_shares
                for lot in lots[instrument_id]
            ),
            ZERO,
        )
        if position_after == 0:
            episodes.append(
                {
                    **episode,
                    "closed_at": event["occurred_at"],
                    "closed_event_id": event["event_id"],
                }
            )
            del episode_state[instrument_id]

    holdings: list[dict[str, Any]] = []
    for instrument_id in sorted(lots):
        open_lots = [
            lot for lot in lots[instrument_id] if lot.remaining_shares > 0
        ]
        total_shares = sum((lot.remaining_shares for lot in open_lots), ZERO)
        if total_shares == 0:
            continue
        cost_basis = money(sum((lot.remaining_cost for lot in open_lots), ZERO))
        first_lot = open_lots[0]
        multiplier = first_lot.contract_multiplier
        avg_cost = price(
            cost_basis / total_shares / multiplier,
            field="avg_cost",
        )
        holdings.append(
            {
                "instrument_id": instrument_id,
                "instrument_type": first_lot.instrument_type,
                "instrument_name": first_lot.instrument_name,
                "quote_symbol": first_lot.quote_symbol,
                "symbol": first_lot.symbol,
                "shares": total_shares,
                "avg_cost": avg_cost,
                "cost_basis": cost_basis,
                "contract_multiplier": multiplier,
            }
        )

    return ReplayResult(
        portfolio=expected_portfolio,
        initial_cash=initial_cash,
        cash=cash,
        cash_flow_total=cash_flow_total,
        income_expense_total=income_expense_total,
        buy_outflow=buy_outflow,
        sell_inflow=sell_inflow,
        realized_pnl_total=realized_total,
        lots=dict(lots),
        holdings=holdings,
        trade_history=history,
        realized_pnl_per_trade=realized_trades,
        closed_episodes=episodes,
        effective_events=effective,
        raw_event_ids={event["event_id"] for event in events},
    )
