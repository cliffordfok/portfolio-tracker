from __future__ import annotations

import unittest
from decimal import Decimal

from portfolio_tracker.errors import BusinessInvariantError
from portfolio_tracker.replay import replay_portfolio

from .helpers import event


class ReplayTests(unittest.TestCase):
    def fixture(self) -> list[dict]:
        return [
            event(1, "PORTFOLIO_OPEN", initial_cash="1000"),
            event(2, "BUY", symbol="AAPL", shares="10", price="10", fee="1"),
            event(3, "BUY", symbol="AAPL", shares="5", price="20", fee="0.5"),
            event(4, "SELL", symbol="AAPL", shares="12", price="30", fee="1.2"),
        ]

    def test_fifo_fee_allocation_and_realized_pnl(self) -> None:
        result = replay_portfolio(self.fixture())
        self.assertEqual(result.realized_pnl_total, Decimal("217.600000"))
        self.assertEqual(result.cash, Decimal("1157.300000"))
        self.assertEqual(result.holdings[0]["shares"], Decimal("3.00000000"))
        self.assertEqual(result.holdings[0]["cost_basis"], Decimal("60.300000"))
        self.assertEqual(result.holdings[0]["avg_cost"], Decimal("20.100000"))
        matches = result.realized_pnl_per_trade[0]["matches"]
        self.assertEqual(matches[0]["sell_fee"], Decimal("1.000000"))
        self.assertEqual(matches[1]["sell_fee"], Decimal("0.200000"))
        self.assertEqual(matches[1]["buy_fee"], Decimal("0.200000"))

    def test_cash_conservation(self) -> None:
        result = replay_portfolio(self.fixture())
        expected = (
            result.initial_cash
            + result.cash_flow_total
            - result.buy_outflow
            + result.sell_inflow
        )
        self.assertEqual(result.cash, expected)

    def test_closed_episode_win_rate(self) -> None:
        events = self.fixture() + [
            event(5, "SELL", symbol="AAPL", shares="3", price="15", fee="0")
        ]
        result = replay_portfolio(events)
        self.assertEqual(len(result.closed_episodes), 1)
        self.assertEqual(result.closed_episodes[0]["pnl"], Decimal("202.300000"))
        self.assertEqual(result.win_rate, Decimal("1.00000000"))

    def test_oversell_rejected(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="1000"),
            event(2, "SELL", symbol="AAPL", shares="1", price="10", fee="0"),
        ]
        with self.assertRaisesRegex(BusinessInvariantError, "oversells"):
            replay_portfolio(events)

    def test_negative_cash_rejected(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="10"),
            event(2, "BUY", symbol="AAPL", shares="2", price="10", fee="0"),
        ]
        with self.assertRaisesRegex(BusinessInvariantError, "cash negative"):
            replay_portfolio(events)

    def test_cash_flow_cannot_make_cash_negative(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="10"),
            event(2, "CASH_FLOW", amount="-11"),
        ]
        with self.assertRaisesRegex(BusinessInvariantError, "cash negative"):
            replay_portfolio(events)

    def test_zero_cash_flow_is_an_exact_noop(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="10"),
            event(2, "CASH_FLOW", amount="0"),
        ]
        result = replay_portfolio(events)
        self.assertEqual(result.cash, Decimal("10.000000"))
        self.assertEqual(result.cash_flow_total, Decimal("0.000000"))

    def test_fractional_trade_preserves_exact_decimal_cash(self) -> None:
        quantity = Decimal("0.12345678")
        unit_price = Decimal("100.123456")
        fee = Decimal("0.01")
        result = replay_portfolio(
            [
                event(1, "PORTFOLIO_OPEN", initial_cash="1000"),
                event(
                    2,
                    "BUY",
                    symbol="AAPL",
                    shares=str(quantity),
                    price=str(unit_price),
                    fee=str(fee),
                ),
            ]
        )
        self.assertEqual(
            result.cash,
            Decimal("1000") - quantity * unit_price - fee,
        )

    def test_backdated_buy_changes_fifo_order_deterministically(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="1000"),
            event(
                2,
                "BUY",
                occurred_at="2024-01-03T15:00:00Z",
                symbol="AAPL",
                shares="1",
                price="20",
                fee="0",
            ),
            event(
                3,
                "BUY",
                occurred_at="2024-01-02T15:00:00Z",
                symbol="AAPL",
                shares="1",
                price="10",
                fee="0",
            ),
            event(
                4,
                "SELL",
                occurred_at="2024-01-04T15:00:00Z",
                symbol="AAPL",
                shares="1",
                price="30",
                fee="0",
            ),
        ]
        result = replay_portfolio(events)
        match = result.realized_pnl_per_trade[0]["matches"][0]
        self.assertEqual(match["buy_event_id"], "paper-3")
        self.assertEqual(match["pnl"], Decimal("20"))

    def test_void_of_past_buy_revalidates_later_sell(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="1000"),
            event(2, "BUY", symbol="AAPL", shares="10", price="10", fee="0"),
            event(3, "SELL", symbol="AAPL", shares="8", price="11", fee="0"),
            event(4, "VOID", void_target="paper-2"),
        ]
        with self.assertRaisesRegex(BusinessInvariantError, "oversells"):
            replay_portfolio(events)

    def test_amended_buy_fee_recomputes_fifo_cost_basis(self) -> None:
        events = [
            event(1, "PORTFOLIO_OPEN", initial_cash="1000"),
            event(2, "BUY", symbol="AAPL", shares="10", price="10", fee="0"),
            event(3, "SELL", symbol="AAPL", shares="1", price="20", fee="0"),
            event(4, "AMEND", amend_target="paper-2", changes={"fee": "10"}),
        ]
        result = replay_portfolio(events)
        self.assertEqual(result.realized_pnl_per_trade[0]["pnl"], Decimal("9"))
        self.assertEqual(result.holdings[0]["cost_basis"], Decimal("99"))

    def test_complex_fixture_conserves_cash_with_corrections(self) -> None:
        events = [event(1, "PORTFOLIO_OPEN", initial_cash="100000")]
        seq = 2
        buy_ids = []
        for index in range(20):
            buy = event(
                seq,
                "BUY",
                symbol="AAPL",
                shares="1",
                price=str(100 + index),
                fee="0.01",
            )
            buy_ids.append(buy["event_id"])
            events.append(buy)
            seq += 1
        for index in range(15):
            events.append(
                event(
                    seq,
                    "SELL",
                    symbol="AAPL",
                    shares="1",
                    price=str(130 + index),
                    fee="0.02",
                )
            )
            seq += 1
        cash_ids = []
        for amount in ("100", "-20", "0"):
            flow = event(seq, "CASH_FLOW", amount=amount)
            cash_ids.append(flow["event_id"])
            events.append(flow)
            seq += 1
        events.extend(
            [
                event(seq, "VOID", void_target=buy_ids[-1]),
                event(seq + 1, "VOID", void_target=cash_ids[1]),
                event(
                    seq + 2,
                    "AMEND",
                    amend_target=buy_ids[0],
                    changes={"fee": "0.03"},
                ),
            ]
        )
        result = replay_portfolio(events)
        effective = result.effective_events
        expected = Decimal("100000")
        for item in effective:
            if item["action"] == "CASH_FLOW":
                expected += Decimal(item["amount"])
            elif item["action"] == "BUY":
                expected -= Decimal(item["shares"]) * Decimal(item["price"])
                expected -= Decimal(item.get("fee", "0"))
            elif item["action"] == "SELL":
                expected += Decimal(item["shares"]) * Decimal(item["price"])
                expected -= Decimal(item.get("fee", "0"))
        self.assertEqual(result.cash, expected)
        self.assertEqual(
            sum(1 for item in events if item["action"] == "BUY"),
            20,
        )
        self.assertEqual(
            sum(1 for item in events if item["action"] == "SELL"),
            15,
        )


if __name__ == "__main__":
    unittest.main()
